"""scripts/db_gateway_install.sh under a temporary root.

The installer puts the database gateway on the RU server. systemd and the
tunnel are stand-ins; the Python environment is this test run's interpreter, so
the installed copy of the code really opens an encrypted database and serves it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from aiohttp.test_utils import unused_port

from db.database import Database
from db.remote import RemoteDatabase

ROOT = Path(__file__).resolve().parents[1]
KEY = "ab" * 32

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the script refuses to run without root"
)

# The system python3: "-m venv" makes an environment whose python is this test
# run's interpreter and whose pip only records the call.
FAKE_PYTHON = f"""#!/bin/bash
if [[ "$1" == "-m" && "$2" == "venv" ]]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<EOF
#!/bin/bash
if [[ "\\$1" == "-m" && "\\$2" == "pip" ]]; then
  echo "pip \\$*" >> "$CALLS"
  exit ${{FAKE_PIP_RC:-0}}
fi
exec "{sys.executable}" "\\$@"
EOF
  chmod +x "$3/bin/python"
  exit 0
fi
exec "{sys.executable}" "$@"
"""

# systemd: "restart" runs the installed gateway in the background.
FAKE_SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$CALLS"
alive() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
halt() { alive && kill "$(cat "$PIDFILE")" && sleep 0.5; rm -f "$PIDFILE"; }
case "$1" in
  is-active)
    if alive; then echo active; else echo inactive; exit 3; fi ;;
  restart)
    halt
    (
      set -a
      . "$PREFIX/etc/vpn-db/gateway.env"
      set +a
      cd "$PREFIX/opt/vpn-db/app" || exit 1
      exec "$PREFIX/opt/vpn-db/venv/bin/python" -m db.gateway >>"$GWLOG" 2>&1
    ) &
    echo $! > "$PIDFILE" ;;
  disable) halt ;;
esac
exit 0
"""
FAKE_IP = (
    '#!/bin/bash\necho "7: wg-db    inet ${FAKE_TUNNEL_ADDR}/30 scope global wg-db"\n'
)


class Box:
    def __init__(self, tmp: Path) -> None:
        self.prefix = tmp / "root"
        self.prefix.mkdir()
        self.src = tmp / "src"
        (self.src / "scripts").mkdir(parents=True)
        (self.src / "deploy").mkdir()
        shutil.copytree(
            ROOT / "db", self.src / "db", ignore=shutil.ignore_patterns("__pycache__")
        )
        shutil.copy(ROOT / "logging_setup.py", self.src)
        shutil.copytree(
            ROOT / "deploy" / "db-gateway", self.src / "deploy" / "db-gateway"
        )
        for name in ("db_gateway_install.sh", "db_tool.py"):
            shutil.copy(ROOT / "scripts" / name, self.src / "scripts")
        self.calls = tmp / "calls.log"
        self.pidfile = tmp / "gateway.pid"
        self.port = unused_port()
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        stubs = {"python3": FAKE_PYTHON, "systemctl": FAKE_SYSTEMCTL, "ip": FAKE_IP}
        for name, body in stubs.items():
            path = bin_dir / name
            path.write_text(body)
            path.chmod(0o755)
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PREFIX": str(self.prefix),
            "SERVICE_USER": "root",
            "PYTHON_BIN": str(bin_dir / "python3"),
            "DB_GATEWAY_HOST": "127.0.0.1",
            "DB_GATEWAY_PORT": str(self.port),
            "FAKE_TUNNEL_ADDR": "127.0.0.1",
            "CALLS": str(self.calls),
            "PIDFILE": str(self.pidfile),
            "GWLOG": str(tmp / "gateway.log"),
        }
        self.etc = self.prefix / "etc" / "vpn-db"
        self.token_file = self.etc / "secrets" / "db_gateway_token"
        self.key_file = self.etc / "secrets" / "db_key"
        self.db_file = self.prefix / "var" / "lib" / "vpn-db" / "bot.db"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(self.src / "scripts" / "db_gateway_install.sh"), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def log(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def stop_gateway(self) -> None:
        if self.pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(self.pidfile.read_text()), signal.SIGTERM)

    def put_database(self, key: str = KEY) -> None:
        """What the cutover copies over: the encrypted file and its key."""

        async def make() -> None:
            database = Database(str(self.db_file), KEY)
            await database.connect()
            await database.create_user(777, "moved", None)
            await database.close()

        asyncio.run(make())
        self.key_file.write_text(key)


@pytest.fixture
def box(tmp_path: Path) -> Iterator[Box]:
    made = Box(tmp_path)
    yield made
    made.stop_gateway()


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_gateway_packages_are_the_bots_versions() -> None:
    def pins(path: Path) -> dict[str, str]:
        lines = (line.strip() for line in path.read_text().splitlines())
        pairs = (line.split("==") for line in lines if "==" in line)
        return {name.split("[")[0]: version for name, version in pairs}

    everything = pins(ROOT / "requirements.txt")
    subset = pins(ROOT / "deploy" / "db-gateway" / "requirements.txt")
    assert set(subset) == {
        "aiohttp",
        "aiosqlite",
        "pydantic",
        "pydantic-settings",
        "sqlcipher3-binary",
    }
    for name, version in subset.items():
        assert everything[name] == version, name


def test_the_unit_and_the_installer_agree_on_paths() -> None:
    unit = (ROOT / "deploy" / "db-gateway" / "node-db-gateway.service").read_text()
    script = (ROOT / "scripts" / "db_gateway_install.sh").read_text()
    assert "ExecStart=/opt/vpn-db/venv/bin/python -m db.gateway" in unit
    assert "WorkingDirectory=/opt/vpn-db/app" in unit
    assert "EnvironmentFile=/etc/vpn-db/gateway.env" in unit
    assert "ReadWritePaths=/var/lib/vpn-db" in unit
    assert "User=vpn-db" in unit
    assert "Requires=wg-quick@wg-db.service" in unit
    for fragment in ('OPT="$PREFIX/opt/vpn-db"', 'ETC="$PREFIX/etc/vpn-db"'):
        assert fragment in script
    assert 'DATA="$PREFIX/var/lib/vpn-db"' in script
    assert 'SERVICE_USER="${SERVICE_USER:-vpn-db}"' in script
    # The service never runs as root and cannot write outside its data.
    assert re.search(r"^User=root", unit, re.MULTILINE) is None
    assert "ProtectSystem=strict" in unit
    assert "NoNewPrivileges=yes" in unit


def test_report_changes_nothing(box: Box) -> None:
    result = box.run()
    assert result.returncode == 0
    assert result.stdout.count("MISSING") >= 5
    assert result.stdout.rstrip().endswith("DB_GATEWAY_REPORT_DONE")
    assert list(box.prefix.iterdir()) == []
    assert box.log() == ["systemctl is-active node-db-gateway.service"]


def test_apply_installs_everything_and_starts_nothing(box: Box) -> None:
    result = box.run("--apply")
    assert result.returncode == 0, result.stderr
    assert "DB_GATEWAY_INSTALLED" in result.stdout
    app = box.prefix / "opt" / "vpn-db" / "app"
    assert (app / "db" / "gateway.py").is_file()
    assert (app / "logging_setup.py").is_file()
    assert (app / "scripts" / "db_tool.py").is_file()
    token = box.token_file.read_text()
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    assert token not in result.stdout + result.stderr
    assert mode(box.token_file) == 0o440
    assert mode(box.etc / "gateway.env") == 0o640
    assert mode(box.db_file.parent) == 0o700
    settings = (box.etc / "gateway.env").read_text()
    assert f"DATABASE_PATH={box.db_file}" in settings
    assert f"SECRETS_DIR={box.etc / 'secrets'}" in settings
    assert "ACTIVITY_LOGGING_ENABLED=false" in settings
    assert "TOKEN" not in settings
    assert (box.prefix / "etc/systemd/system/node-db-gateway.service").is_file()
    log = box.log()
    assert any(
        line.startswith("pip ") and "deploy/db-gateway/requirements.txt" in line
        for line in log
    )
    assert "systemctl daemon-reload" in log
    assert not any("restart" in line or "enable" in line for line in log)


def test_apply_refuses_an_incomplete_copy_and_a_failed_download(box: Box) -> None:
    (box.src / "db" / "gateway.py").unlink()
    result = box.run("--apply")
    assert result.returncode == 1
    assert "the copy is incomplete" in result.stderr
    assert list(box.prefix.iterdir()) == []
    shutil.copy(ROOT / "db" / "gateway.py", box.src / "db")
    result = box.run("--apply", FAKE_PIP_RC="1")
    assert result.returncode == 1
    assert "pypi.org" in result.stderr
    assert not box.token_file.exists()


def test_start_refuses_until_the_data_the_key_and_the_tunnel_are_there(
    box: Box,
) -> None:
    assert "run --apply first" in box.run("--start").stderr
    box.run("--apply")
    assert "database file is missing or empty" in box.run("--start").stderr
    box.db_file.touch()
    assert "database file is missing or empty" in box.run("--start").stderr
    box.put_database()
    box.key_file.unlink()
    assert "database key is missing" in box.run("--start").stderr
    box.key_file.write_text("abcd")
    assert "64 hex characters" in box.run("--start").stderr
    box.key_file.write_text(KEY)
    down = box.run("--start", FAKE_TUNNEL_ADDR="10.9.9.9")
    assert "tunnel address 127.0.0.1 is not up" in down.stderr
    box.key_file.write_text("cd" * 32)
    wrong = box.run("--start")
    assert wrong.returncode == 1
    assert "key does not open the database file" in wrong.stderr
    assert not any("restart" in line for line in box.log())


async def read_moved_user(box: Box) -> tuple[str, bool]:
    client = RemoteDatabase(f"http://127.0.0.1:{box.port}", box.token_file.read_text())
    await client.connect()
    try:
        user = await client.get_user(777)
        assert user is not None
        return user["username"], client.encrypted
    finally:
        await client.close()


def test_the_moved_database_is_served_to_the_bots_client(box: Box) -> None:
    box.run("--apply")
    box.put_database()
    started = box.run("--start")
    assert started.returncode == 0, started.stderr
    assert "database check: ok" in started.stdout
    assert "DB_GATEWAY_STARTED" in started.stdout
    assert mode(box.db_file) == 0o600
    assert mode(box.key_file) == 0o440
    assert box.run("--status").stdout.startswith("OK gateway")
    assert asyncio.run(read_moved_user(box)) == ("moved", True)

    # A second --apply updates the code and keeps what must not change.
    token = box.token_file.read_text()
    (box.etc / "gateway.env").write_text(
        (box.etc / "gateway.env").read_text() + "LOG_LEVEL=WARNING\n"
    )
    again = box.run("--apply")
    assert again.returncode == 0, again.stderr
    assert "service: restarted with the new code" in again.stdout
    assert box.token_file.read_text() == token
    assert "LOG_LEVEL=WARNING" in (box.etc / "gateway.env").read_text()
    assert box.db_file.stat().st_size > 0

    halted = box.run("--stop")
    assert "DB_GATEWAY_HALTED" in halted.stdout
    assert box.run("--status").stdout.startswith("WARN gateway")
    assert box.db_file.stat().st_size > 0
    assert box.key_file.exists()


def test_unknown_arguments_are_refused(box: Box) -> None:
    result = box.run("--nope")
    assert result.returncode == 1
    assert "usage" in result.stderr
