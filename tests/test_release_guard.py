"""Signed releases: only code the owner signed reaches the servers.

Real git repositories and real SSH keys; nothing is faked but the paths.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "release_guard.sh"

SSH_KEYGEN = shutil.which("ssh-keygen") or "ssh-keygen"
pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"
)


def sh(*args: str, cwd: Path | None = None, **kw: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        **kw,
    }
    return subprocess.run(  # noqa: S603
        list(args), cwd=cwd, env=env, capture_output=True, text=True, check=True
    ).stdout


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"  # where commits are made (GitHub, or an attacker)
        self.repo = tmp / "server-src"  # /root/vpn-src
        self.etc = tmp / "etc"
        self.bin = tmp / "vpn-release-guard"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh("git", "clone", "-q", str(self.origin), str(self.work))
        self.commit("first")
        sh("git", "clone", "-q", str(self.origin), str(self.repo))
        self.owner = tmp / "owner"
        self.stranger = tmp / "stranger"
        for key in (self.owner, self.stranger):
            sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key))
        self.env = {
            "RELEASE_REPO": str(self.repo),
            "RELEASE_ETC": str(self.etc),
            "RELEASE_BIN": str(self.bin),
        }

    def commit(self, text: str, *, force: bool = False) -> str:
        (self.work / "file.txt").write_text(text)
        sh("git", "add", "-A", cwd=self.work)
        sh("git", "commit", "-q", "-m", text, cwd=self.work)
        push = ["git", "push", "-q", "origin", "HEAD:main"]
        if force:
            push.insert(2, "--force")
        sh(*push, cwd=self.work)
        return sh("git", "rev-parse", "HEAD", cwd=self.work).strip()

    def guard(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(GUARD), *args],
            env={**os.environ, **self.env},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def sign(self, sha: str, key: Path | None = None) -> str:
        return subprocess.run(  # noqa: S603
            [
                SSH_KEYGEN,
                "-Y",
                "sign",
                "-f",
                str(key or self.owner),
                "-n",
                "vpn-release",
            ],
            input=f"vpn-release\n{sha}\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def head(self) -> str:
        return sh("git", "rev-parse", "HEAD", cwd=self.repo).strip()


@pytest.fixture
def world(tmp_path: Path) -> World:
    made = World(tmp_path)
    done = made.guard("--install", str(made.owner) + ".pub")
    assert done.returncode == 0, done.stderr
    return made


def test_install_trusts_the_code_on_the_server_now(world: World) -> None:
    assert (
        (world.etc / "allowed_signers")
        .read_text()
        .startswith('owner namespaces="vpn-release" ssh-ed25519 ')
    )
    assert (world.etc / "current").read_text().strip() == world.head()
    assert world.bin.exists()
    assert "OK release guard" in world.guard("--status").stdout
    assert world.guard("--verify-head").returncode == 0


def test_a_signed_release_is_taken(world: World) -> None:
    sha = world.commit("second")
    fetched = world.guard("--fetch")
    assert fetched.returncode == 0, fetched.stderr
    assert f"RELEASE_CANDIDATE {sha}" in fetched.stdout
    assert "second" in fetched.stdout
    assert world.head() != sha  # fetching moves nothing
    done = world.guard("--apply", stdin=world.sign(sha))
    assert done.returncode == 0, done.stderr
    assert "RELEASE_OK" in done.stdout
    assert world.head() == sha
    assert world.guard("--verify-head").returncode == 0


def test_a_stranger_s_signature_or_none_changes_nothing(world: World) -> None:
    before = world.head()
    sha = world.commit("evil")
    world.guard("--fetch")
    stranger = world.guard("--apply", stdin=world.sign(sha, world.stranger))
    assert stranger.returncode == 1
    assert "not the owner's" in stranger.stderr
    empty = world.guard("--apply", stdin="")
    assert empty.returncode == 1
    assert world.head() == before


def test_a_signature_for_another_commit_does_not_carry_over(world: World) -> None:
    good = world.commit("reviewed")
    signature = world.sign(good)
    world.commit("pushed on top afterwards")
    world.guard("--fetch")
    late = world.guard("--apply", stdin=signature)
    assert late.returncode == 1
    assert "not the owner's" in late.stderr


def test_rewritten_history_is_refused_even_when_signed(world: World) -> None:
    world.commit("second")
    sha = world.commit("third")
    world.guard("--fetch")
    assert world.guard("--apply", stdin=world.sign(sha)).returncode == 0
    # Now someone rewrites main to a commit that does not continue the release.
    sh("git", "reset", "-q", "--hard", "HEAD~2", cwd=world.work)
    other = world.commit("a different past", force=True)
    fetched = world.guard("--fetch")
    assert "WARNING: this commit does not continue" in fetched.stdout
    refused = world.guard("--apply", stdin=world.sign(other))
    assert refused.returncode == 1
    assert "does not continue" in refused.stderr


def test_a_plain_git_pull_is_caught_by_the_update_scripts(world: World) -> None:
    world.commit("pulled without signing")
    sh("git", "pull", "-q", "--ff-only", cwd=world.repo)
    verify = world.guard("--verify-head")
    assert verify.returncode == 1
    assert "not the signed release" in verify.stderr
    assert "NOT the signed release" in world.guard("--status").stdout


def test_edits_in_the_checkout_are_not_signed_either(world: World) -> None:
    (world.repo / "file.txt").write_text("edited on the server")
    verify = world.guard("--verify-head")
    assert verify.returncode == 1
    assert "changes nobody signed" in verify.stderr


def test_without_the_guard_nothing_is_enforced(tmp_path: Path) -> None:
    made = World(tmp_path)
    assert made.guard("--verify-head").returncode == 0
    assert "not installed" in made.guard("--status").stdout


def test_install_wants_a_public_key(world: World, tmp_path: Path) -> None:
    bad = tmp_path / "bad.pub"
    bad.write_text("not a key\n")
    done = world.guard("--install", str(bad))
    assert done.returncode == 1
    assert "not an SSH public key" in done.stderr


def test_update_bot_refuses_code_that_is_not_the_signed_release(
    tmp_path: Path,
) -> None:
    from tests.test_database_switch import Box

    box = Box(tmp_path)
    stub = tmp_path / "guard"
    stub.write_text('#!/bin/bash\necho "RELEASE_STOPPED: unsigned $*" >&2\nexit 1\n')
    stub.chmod(0o755)
    source = tmp_path / "src"
    source.mkdir()
    (source / "Dockerfile").write_text("FROM scratch\n")
    (source / "main.py").write_text("")
    done = box.run(str(source), "--apply", RELEASE_GUARD_BIN=str(stub))
    assert done.returncode == 1
    assert f"unsigned --verify-head {source}" in done.stderr
    log = box.calls.read_text() if box.calls.exists() else ""
    assert "build" not in log
