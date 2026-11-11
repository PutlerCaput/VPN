"""update_bot.sh --database-gateway / --database-local with a stand-in docker.

Pointing the bot at the database on the other server is one command, and it
must take itself back when the bot does not come up healthy on it. The access
token arrives on standard input and is never shown.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "update_bot.sh"
TOKEN = "0123456789abcdef" * 4
URL = "http://10.204.0.1:8444"

# Healthy unless the bot is on the gateway and the gateway is "down".
DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$CALLS"
case "$*" in
  "inspect -f {{.Config.Image}}"*) echo "node-rental-bot:latest" ;;
  *com.docker.compose.project.working_dir*) echo "$WORKDIR" ;;
  *com.docker.compose.project*) echo "node-rental" ;;
  *com.docker.compose.service*) echo "bot" ;;
  *State.Health*)
    remote=no
    grep -q '^DATABASE_URL=.' "$WORKDIR/.env" && remote=yes
    if [[ "$FAKE_GATEWAY" == down && "$remote" == yes ]]; then
      echo starting
    elif [[ "$FAKE_LOCAL" == broken && "$remote" == no ]]; then
      echo starting
    else
      echo healthy
    fi ;;
esac
exit 0
"""


class Box:
    def __init__(self, tmp: Path) -> None:
        self.workdir = tmp / "node-rental"
        (self.workdir / "data").mkdir(parents=True)
        (self.workdir / "data" / "bot.db").write_bytes(b"old copy")
        self.env_file = self.workdir / ".env"
        self.env_file.write_text("BOT_USERNAME=test_bot\nENABLED_TIERS=standard\n")
        self.calls = tmp / "calls.log"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        for name, body in {"docker": DOCKER, "sleep": "#!/bin/bash\nexit 0\n"}.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "WORKDIR": str(self.workdir),
            "CALLS": str(self.calls),
            "HEALTH_TIMEOUT": "0",
            "FAKE_GATEWAY": "up",
            "FAKE_LOCAL": "ok",
        }

    def run(
        self, *args: str, stdin: str = "", **env: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(SCRIPT), *args],
            env={**self.env, **env},
            input=stdin,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def settings(self) -> dict[str, str]:
        pairs = (line.split("=", 1) for line in self.env_file.read_text().splitlines())
        return dict(pairs)

    def recreated(self) -> int:
        log = self.calls.read_text() if self.calls.exists() else ""
        return log.count("docker compose -p node-rental up -d")


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def switch(box: Box, **env: str) -> subprocess.CompletedProcess[str]:
    # HEALTH_TIMEOUT=0 would never look at the container: one look is enough here.
    return box.run(
        "--database-gateway", URL, stdin=TOKEN + "\n", **{"HEALTH_TIMEOUT": "1", **env}
    )


def test_the_bot_is_pointed_at_the_gateway(box: Box) -> None:
    result = switch(box)
    assert result.returncode == 0, result.stderr
    assert "SET_OK DATABASE_URL DB_GATEWAY_TOKEN" in result.stdout
    # The token is in the settings file and nowhere on the screen.
    assert TOKEN not in result.stdout + result.stderr
    assert box.settings() == {
        "BOT_USERNAME": "test_bot",
        "ENABLED_TIERS": "standard",
        "DATABASE_URL": URL,
        "DB_GATEWAY_TOKEN": TOKEN,
    }
    assert stat.S_IMODE(box.env_file.stat().st_mode) == 0o600
    assert box.recreated() == 1


def test_an_unreachable_gateway_takes_the_switch_back(box: Box) -> None:
    before = box.env_file.read_text()
    result = switch(box, FAKE_GATEWAY="down")
    assert result.returncode == 1
    assert "previous .env restored (healthy)" in result.stderr
    assert box.env_file.read_text() == before
    assert box.recreated() == 2
    assert TOKEN not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://45.138.157.65:8444",
        "https://10.204.0.1:8444",
        "http://10.204.0.1",
        "http://10.204.0.1:8444/x",
        "http://10.204.0.1:8444;reboot",
        "http://db.example.com:8444",
    ],
)
def test_only_a_tunnel_address_is_accepted(box: Box, url: str) -> None:
    result = box.run("--database-gateway", url, stdin=TOKEN + "\n")
    assert result.returncode == 1
    assert "inside the tunnel" in result.stderr
    assert "DATABASE_URL" not in box.settings()
    assert box.recreated() == 0


@pytest.mark.parametrize(
    "token", ["", "short", "has spaces " * 4, "a" * 31, "x$(id)" * 8]
)
def test_a_missing_or_odd_token_is_refused(box: Box, token: str) -> None:
    result = box.run("--database-gateway", URL, stdin=token + "\n")
    assert result.returncode == 1
    assert "no token on standard input" in result.stderr
    assert "DATABASE_URL" not in box.settings()
    assert box.recreated() == 0


def test_plain_set_cannot_write_the_database_settings(box: Box) -> None:
    for pair in (f"DATABASE_URL={URL}", f"DB_GATEWAY_TOKEN={TOKEN}"):
        result = box.run("--set", pair)
        assert result.returncode == 1
        assert "not allowed" in result.stderr
    assert "DATABASE_URL" not in box.settings()


def test_back_to_the_local_file(box: Box) -> None:
    switch(box)
    result = box.run("--database-local", HEALTH_TIMEOUT="1")
    assert result.returncode == 0, result.stderr
    assert "DATABASE_LOCAL_OK" in result.stdout
    assert "NOT in this file" in result.stdout
    assert box.settings() == {"BOT_USERNAME": "test_bot", "ENABLED_TIERS": "standard"}
    assert TOKEN not in result.stdout + result.stderr


def test_back_to_local_is_refused_when_there_is_nothing_to_go_back_to(
    box: Box,
) -> None:
    already = box.run("--database-local")
    assert already.returncode == 1
    assert "already uses the local file" in already.stderr
    switch(box)
    (box.workdir / "data" / "bot.db").unlink()
    gone = box.run("--database-local")
    assert gone.returncode == 1
    assert "no local file to go back to" in gone.stderr
    assert box.settings()["DATABASE_URL"] == URL


def test_a_broken_local_file_keeps_the_bot_on_the_gateway(box: Box) -> None:
    switch(box)
    result = box.run("--database-local", HEALTH_TIMEOUT="1", FAKE_LOCAL="broken")
    assert result.returncode == 1
    assert "gateway settings are back (healthy)" in result.stderr
    assert box.settings()["DATABASE_URL"] == URL
    assert box.settings()["DB_GATEWAY_TOKEN"] == TOKEN
