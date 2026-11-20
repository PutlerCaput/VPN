"""Exercise both CLI modes in disposable repositories with synthetic bytes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.secret_scan import _reviewed_fixture, scan_file

CANARY = "unit-canary-" + "a9" * 20
ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")
assert GIT is not None


@pytest.fixture
def scan_repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    for name in ("secret_scan.py", "secret_scan.sh", "secret_scan_allowlist.json"):
        shutil.copy(ROOT / "scripts" / name, tmp_path / "scripts" / name)
    subprocess.run([GIT, "init", "-q", str(tmp_path)], check=True)  # noqa: S603
    return tmp_path


def _run(root: Path, explicit: str | None = None) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(root / "scripts/secret_scan.py")]
    if explicit is not None:
        argv.append(explicit)
    return subprocess.run(argv, cwd=root, capture_output=True, text=True, check=False)  # noqa: S603


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "name",
    [
        "docker-inspect(1).json",
        "inspect.json",
        "renamed with space.txt",
        "strange\nname.json",
    ],
)
def test_renamed_runtime_dump_is_blocked_in_both_modes(
    scan_repo: Path, name: str, explicit: bool
) -> None:
    data = [{"Config": {"Env": ["BOT_TOKEN=" + CANARY], "Cmd": ["--token", CANARY]}}]
    (scan_repo / name).write_text(json.dumps(data))
    result = _run(scan_repo, name if explicit else None)
    assert result.returncode == 1
    assert CANARY not in result.stdout + result.stderr
    assert "secret scan: clean" not in result.stdout
    if "\n" in name:
        assert "strange\\nname.json" in result.stderr


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize(
    "name",
    [".env.production", "data/bot.db", "snapshot.zip", "secrets/bot.txt", "old.key"],
)
def test_tracked_artifacts_cannot_hide_behind_gitignore(
    scan_repo: Path, name: str, explicit: bool
) -> None:
    (scan_repo / ".gitignore").write_text(name + "\n")
    path = scan_repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private bytes\0" + CANARY.encode())
    subprocess.run([GIT, "add", "-f", "--", name], cwd=scan_repo, check=True)  # noqa: S603
    result = _run(scan_repo, name if explicit else None)
    assert result.returncode == 1
    assert CANARY not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("settings.json", json.dumps({"password": CANARY})),
        ("settings.yml", "api_key: " + CANARY),
        ("settings.env", "PRIVATE_KEY=" + "AbCdEfGh" * 8),
        ("launch.sh", "cloudflared tunnel run --token " + CANARY),
        ("launch.txt", "cloudflared --token=" + CANARY),
        ("runtime.py", 'password = "' + CANARY + '"'),
    ],
)
def test_literal_credentials_are_detected_without_echo(
    scan_repo: Path, name: str, content: str
) -> None:
    (scan_repo / name).write_text(content)
    result = _run(scan_repo, name)
    assert result.returncode == 1
    assert CANARY not in result.stdout + result.stderr
    assert "AbCdEfGh" not in result.stdout + result.stderr


def test_missing_file_is_error_not_clean(scan_repo: Path) -> None:
    result = _run(scan_repo, "missing.txt")
    assert result.returncode == 2
    assert "INPUT_ERROR" in result.stderr
    assert "clean" not in result.stdout


def test_unreadable_file_is_error_not_clean(
    scan_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts.secret_scan import main

    def denied(path: Path) -> bytes:
        raise PermissionError(CANARY)

    monkeypatch.setattr(Path, "read_bytes", denied)
    assert main([str(scan_repo / "denied.txt")]) == 2
    result = capsys.readouterr()
    assert CANARY not in result.err
    assert "clean" not in result.out


def test_failed_git_enumeration_is_error(scan_repo: Path) -> None:
    shutil.rmtree(scan_repo / ".git")
    result = _run(scan_repo)
    assert result.returncode == 2
    assert "clean" not in result.stdout


def test_templates_references_and_placeholders_are_allowed(scan_repo: Path) -> None:
    (scan_repo / ".env.example").write_text(
        'BOT_TOKEN=\nAPI_KEY=${API_KEY}\nPASSWORD="<set privately>"\n'
    )
    (scan_repo / "settings.json").write_text(
        json.dumps({"password": "test-placeholder"})
    )
    (scan_repo / "app.py").write_text(
        "token: SecretStr\npassword = settings.password\n"
    )
    assert _run(scan_repo).returncode == 0


def test_empty_fixture_allowance_is_not_directory_exclusion(tmp_path: Path) -> None:
    path = tmp_path / "tests/test_leaked.py"
    path.parent.mkdir()
    path.write_text('password = "' + CANARY + '"')
    assert scan_file(path)


def test_reviewed_fixture_allowance_is_exact() -> None:
    path = ROOT / "tests/test_cryptopay.py"
    line = next(
        line for line in path.read_text().splitlines() if line.startswith("TOKEN =")
    )
    assert _reviewed_fixture(path, line)
    assert not _reviewed_fixture(path, line + " ")
    assert not _reviewed_fixture(ROOT / "services/cryptopay.py", line)


def test_symlink_does_not_read_outside_checkout(
    scan_repo: Path, tmp_path: Path
) -> None:
    target = tmp_path / "outside.txt"
    target.write_text(CANARY)
    (scan_repo / "link.txt").symlink_to(target)
    result = _run(scan_repo, "link.txt")
    assert result.returncode == 2
    assert CANARY not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"SQLite format 3\0canary", 1),
        (b"PK\x03\x04canary", 1),
        (b"\xff\x00unknown", 2),
        (b"\xffinvalid-text", 2),
    ],
)
def test_renamed_binary_artifacts_and_unscannable_input(
    scan_repo: Path, payload: bytes, code: int
) -> None:
    (scan_repo / "renamed.txt").write_bytes(payload)
    result = _run(scan_repo, "renamed.txt")
    assert result.returncode == code
    assert "clean" not in result.stdout
