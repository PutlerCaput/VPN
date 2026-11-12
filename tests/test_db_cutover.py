# ruff: noqa: E501  (the stand-in shell scripts below read better unwrapped)
"""A rehearsal of moving the database, with both servers on this machine.

scripts/db_cutover.sh runs on the owner's phone and reaches the servers over
ssh. Here ssh is a stand-in that runs the command locally in one of two
sandboxes, "DE" and "RU". docker, systemd, WireGuard and iptables are
stand-ins too; the scripts under test, the encrypted database, the gateway
process and the bot's client are real.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
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

ROOT = Path(__file__).resolve().parents[1]
KEY = "ab" * 32
DE_PUBLIC_KEY = "dPublicPublicPublicPublicPublicPublicPublic="
OWNER_KEY = "age1" + "q" * 58
HAS_AGE = shutil.which("age") is not None
RU_PUBLIC_KEY = "rPublicPublicPublicPublicPublicPublicPublic="

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the scripts refuse to run without root"
)

SSH = """#!/bin/bash
# ssh [-o option]... target command: run the command in that server's sandbox.
while [[ "$1" == -o ]]; do shift 2; done
target="$1"
shift
echo "$target: $*" >> "$T/ssh.log"
case "$target" in
  de) side=de ;;
  root@45.138.157.65) side=ru ;;
  *) echo "ssh: unknown host $target" >&2; exit 255 ;;
esac
[[ "$FAKE_DOWN" == "$side" ]] && { echo "ssh: connect to host: timed out" >&2; exit 255; }
export SIDE_DIR="$T/$side" PATH="$T/$side/bin:$T/common:$BASE_PATH"
export WG_DIR="$T/$side/wireguard" CALLS="$T/$side/calls.log"
if [[ "$side" == ru ]]; then
  export PREFIX="$T/ru/root" ETC_DIR="$T/ru/root/etc" SERVICE_USER=root
  export PYTHON_BIN="$T/ru/bin/python3" DB_GATEWAY_HOST=127.0.0.1
fi
exec /bin/bash -c "$*"
"""

# Shared by both sandboxes: WireGuard and friends.
WG = """#!/bin/bash
me="$(basename "$SIDE_DIR")"
case "$1" in
  genkey) echo "${me}PrivatePrivatePrivatePrivatePrivatePrivate0=" | cut -c1-44 ;;
  pubkey) cat >/dev/null; [[ "$me" == de ]] && echo "$DE_PUBLIC_KEY" || echo "$RU_PUBLIC_KEY" ;;
  show)
    # A handshake exists once both sides brought the tunnel up.
    if [[ -f "$T/de/tunnel-up" && -f "$T/ru/tunnel-up" && "$FAKE_NO_HANDSHAKE" != 1 ]]; then
      printf 'peer\\t%s\\n' "$(date +%s)"
    fi ;;
esac
"""
IP = """#!/bin/bash
me="$(basename "$SIDE_DIR")"
if [[ "$1" == link ]]; then
  [[ -f "$SIDE_DIR/tunnel-up" ]]
  exit
fi
[[ "$me" == de ]] && public=89.125.210.172 || public=45.138.157.65
echo "2: eth0    inet $public/24 scope global eth0"
# The tunnel address the gateway listens on (loopback in this rehearsal).
[[ "$me" == ru && -f "$SIDE_DIR/tunnel-up" ]] && echo "7: wg-db    inet 127.0.0.1/30 scope global wg-db"
exit 0
"""
SYSTEMCTL = """#!/bin/bash
echo "systemctl $*" >> "$CALLS"
PIDFILE="$T/gateway.pid"
alive() { [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
halt() { alive && kill "$(cat "$PIDFILE")" && sleep 0.5; rm -f "$PIDFILE"; }
case "$*" in
  *wg-quick@wg-db*)
    case "$1" in
      restart) touch "$SIDE_DIR/tunnel-up" ;;
      disable) rm -f "$SIDE_DIR/tunnel-up" ;;
    esac ;;
  "is-active node-db-gateway.service")
    if alive; then echo active; else echo inactive; exit 3; fi ;;
  "restart node-db-gateway.service")
    halt
    (
      set -a
      . "$PREFIX/etc/vpn-db/gateway.env"
      set +a
      cd "$PREFIX/opt/vpn-db/app" || exit 1
      exec "$PREFIX/opt/vpn-db/venv/bin/python" -m db.gateway >>"$T/gateway.log" 2>&1
    ) &
    echo $! > "$PIDFILE" ;;
  "disable --now node-db-gateway.service") halt ;;
esac
exit 0
"""
COMMON = {
    "wg": WG,
    "wg-quick": "#!/bin/bash\nexit 0\n",
    "ip": IP,
    "ss": "#!/bin/bash\nexit 0\n",
    "ping": "#!/bin/bash\nexit 0\n",
    "systemctl": SYSTEMCTL,
    "sleep": "#!/bin/bash\nexit 0\n",
}

# The bot is "healthy" on the gateway only if the bot's own client can read the
# moved data through it with the token from .env.
PROBE = """
import asyncio, sys
from db.remote import RemoteDatabase

async def main(url, token):
    client = RemoteDatabase(url, token, retries=0)
    try:
        await client.connect()
        user = await client.get_user(777)
        assert user and user["username"] == "moved"
    finally:
        await client.close()

asyncio.run(main(sys.argv[1], sys.argv[2]))
"""
DOCKER = f"""#!/bin/bash
echo "docker $*" >> "$CALLS"
W="$T/de/work"
STATE="$T/de/bot-state"
setting() {{ grep "^$1=" "$W/.env" | tail -1 | cut -d= -f2-; }}
case "$*" in
  "inspect -f {{{{.Config.Image}}}}"*) echo "node-rental-bot:latest" ;;
  *com.docker.compose.project.working_dir*) echo "$W" ;;
  *com.docker.compose.project*) echo "node-rental" ;;
  *com.docker.compose.service*) echo "bot" ;;
  *State.Health*)
    if [[ "$(cat "$STATE")" != running ]]; then
      echo exited
    elif [[ -z "$(setting DATABASE_URL)" ]]; then
      echo healthy
    elif [[ "$FAKE_BOT_CANNOT_REACH" != 1 ]] && (cd "$T/de/src" && "{sys.executable}" \\
        -c "$PROBE" "$(setting DATABASE_URL)" "$(cat "$W/secrets/db_gateway_token.txt" 2>/dev/null)") 2>>"$T/probe.log"; then
      echo healthy
    else
      echo starting
    fi ;;
  stop*) echo stopped > "$STATE" ;;
  start*|restart*|compose*) echo running > "$STATE" ;;
  exec*"import db.remote"*) [[ "$FAKE_OLD_BOT" != 1 ]] || exit 1 ;;
  exec*"--policy-keeps-connection-records"*)
    # The check arrives on standard input, as in production. The policy it reads
    # is the one inside the bot's image: an old release still has the old text.
    policy="$T/de/src/legal/privacy.md"
    [[ "$FAKE_OLD_POLICY" != 1 ]] || policy="$T/de/old-privacy.md"
    DOCTOR_PRIVACY_PATH="$policy" "{sys.executable}" - --policy-keeps-connection-records ;;
  run*"db_tool.py backup"*)
    mkdir -p "$T/de/keydir" && cp "$W/secrets/db_key.txt" "$T/de/keydir/db_key"
    cd "$T/de/src" && SECRETS_DIR="$T/de/keydir" "{sys.executable}" scripts/db_tool.py \\
      backup "$W/data/bot.db" "$W/data/move-to-ru.db" ;;
esac
"""
UFW = '#!/bin/bash\necho "ufw $*" >> "$CALLS"\n[[ "$1" == status ]] && echo "Status: active"\nexit 0\n'

IPTABLES = """#!/bin/bash
RULES="$SIDE_DIR/input.rules"
POLICY="$SIDE_DIR/input.policy"
case "$1" in
  -S) echo "-P INPUT $(cat "$POLICY" 2>/dev/null || echo ACCEPT)"; cat "$RULES" 2>/dev/null ;;
  -F) : > "$RULES" ;;
  -A) echo "$*" >> "$RULES" ;;
  -P) echo "$3" > "$POLICY" ;;
esac
exit 0
"""
FAKE_PYTHON = f"""#!/bin/bash
if [[ "$1" == "-m" && "$2" == "venv" ]]; then
  mkdir -p "$3/bin"
  {{
    echo '#!/bin/bash'
    echo '[[ "$1" == -m && "$2" == pip ]] && exit 0'
    echo 'exec "{sys.executable}" "$@"'
  }} > "$3/bin/python"
  chmod +x "$3/bin/python"
  exit 0
fi
exec "{sys.executable}" "$@"
"""
RU_STUBS = {
    "python3": FAKE_PYTHON,
    "iptables": IPTABLES,
    "iptables-save": """#!/bin/bash
echo "# saved"
cat "$SIDE_DIR/input.rules" 2>/dev/null
exit 0
""",
    "iptables-restore": "#!/bin/bash\ncat >/dev/null\n",
    "systemd-run": '#!/bin/bash\necho "systemd-run $*" >> "$CALLS"\n',
    # The orchestrator's next ssh is the "new login" that --confirm asks for.
    "journalctl": '#!/bin/bash\necho "Accepted publickey for root from 203.0.113.5"\n',
    "sha256sum": """#!/bin/bash
if [[ "$FAKE_CORRUPT" == 1 ]]; then
  printf '%064d  %s\\n' 0 "$1"
else
  exec /usr/bin/sha256sum "$@"
fi
""",
}


class Rehearsal:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.port = unused_port()
        self.work = tmp / "de" / "work"
        self.de_src = tmp / "de" / "src"
        self.ru_root = tmp / "ru" / "root"
        self.ru_data = self.ru_root / "var" / "lib" / "vpn-db"
        self.ru_secrets = self.ru_root / "etc" / "vpn-db" / "secrets"
        for folder in ("de/bin", "ru/bin", "common", "de/work/data", "de/work/secrets"):
            (tmp / folder).mkdir(parents=True)
        self.ru_root.mkdir()
        self._write(tmp / "common", COMMON)
        self._write(tmp / "de" / "bin", {"docker": DOCKER, "ufw": UFW})
        self._write(tmp / "ru" / "bin", RU_STUBS)
        # DE's backup settings hold the owner's PUBLIC key; the archive on RU uses it.
        (tmp / "de" / "node-backup.env").write_text(
            f'BACKUP_DIR=/root/server-backups\nAGE_RECIPIENT="{OWNER_KEY}"\n'
        )
        self._write(tmp / "phone", {"ssh": SSH})
        # DE: the checkout the scripts are taken from, and the bot's folder.
        for part in ("db", "deploy/db-gateway", "scripts", "legal"):
            shutil.copytree(
                ROOT / part,
                self.de_src / part,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        shutil.copy(ROOT / "logging_setup.py", self.de_src)
        # The bot's folder as the compose change leaves it: the secret is declared and its
        # file exists (empty means the local file, as init_secrets.sh makes it).
        shutil.copy(ROOT / "docker-compose.yml", self.work / "docker-compose.yml")
        (self.work / "secrets" / "db_gateway_token.txt").write_text("")
        (self.work / ".env").write_text("BOT_USERNAME=test_bot\n")
        (self.work / "secrets" / "db_key.txt").write_text(KEY + "\n")
        (tmp / "de" / "bot-state").write_text("running\n")
        (tmp / "de" / "old-privacy.md").write_text(
            "3.2. Оператор **не обрабатывает**: адреса посещённых ресурсов; "
            "журнал подключений; данные банковских карт.\n"
        )
        asyncio.run(self._make_database())
        self.env = {
            "PATH": f"{tmp / 'phone'}:{os.environ['PATH']}",
            "BASE_PATH": os.environ["PATH"],
            "T": str(tmp),
            "PROBE": PROBE,
            "DE_PUBLIC_KEY": DE_PUBLIC_KEY,
            "RU_PUBLIC_KEY": RU_PUBLIC_KEY,
            "DE_SRC": str(self.de_src),
            "RU_SRC": str(tmp / "ru" / "src"),
            "RU_DATA": str(self.ru_data),
            "RU_SECRETS": str(self.ru_secrets),
            "RU_FIREWALL": str(tmp / "ru" / "ru_firewall.sh"),
            "GATEWAY_URL": f"http://127.0.0.1:{self.port}",
            "DB_GATEWAY_PORT": str(self.port),
            "POLL_SECONDS": "0",
            "HEALTH_TIMEOUT": "1",
            "BACKUP_ENV_DE": str(tmp / "de" / "node-backup.env"),
        }

    @staticmethod
    def _write(folder: Path, stubs: dict[str, str]) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        for name, body in stubs.items():
            path = folder / name
            path.write_text(body)
            path.chmod(0o755)

    async def _make_database(self) -> None:
        database = Database(str(self.work / "data" / "bot.db"), KEY)
        await database.connect()
        await database.create_user(777, "moved", None)
        await database.close()

    def run(self, *step: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/bash", str(ROOT / "scripts" / "db_cutover.sh"), *step],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def settings(self) -> dict[str, str]:
        lines = (self.work / ".env").read_text().splitlines()
        return dict(line.split("=", 1) for line in lines)

    def files_holding(self, text: str) -> list[str]:
        """The files of the bot's folder that contain text, by relative name."""
        needle = text.encode()
        return sorted(
            str(path.relative_to(self.work))
            for path in self.work.rglob("*")
            if path.is_file() and needle in path.read_bytes()
        )

    def bot(self) -> str:
        return (self.tmp / "de" / "bot-state").read_text().strip()

    def gateway_running(self) -> bool:
        pidfile = self.tmp / "gateway.pid"
        if not pidfile.exists():
            return False
        try:
            os.kill(int(pidfile.read_text()), 0)
        except (ProcessLookupError, ValueError):
            return False
        return True

    def close(self) -> None:
        pidfile = self.tmp / "gateway.pid"
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pidfile.read_text()), signal.SIGTERM)


@pytest.fixture
def lab(tmp_path: Path) -> Iterator[Rehearsal]:
    rehearsal = Rehearsal(tmp_path)
    yield rehearsal
    rehearsal.close()


def test_the_whole_move_and_the_way_back(lab: Rehearsal) -> None:
    before = lab.run("check")
    assert before.returncode == 0, before.stderr
    assert "database: local file" in before.stdout
    assert "bot release: knows the gateway" in before.stdout
    assert "not copied yet" in before.stdout

    prepared = lab.run("prepare")
    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    assert "CUTOVER_PREPARED" in prepared.stdout
    # Each side holds the other side's public key; private keys never travelled.
    de_conf = (lab.tmp / "de" / "wireguard" / "wg-db.conf").read_text()
    ru_conf = (lab.tmp / "ru" / "wireguard" / "wg-db.conf").read_text()
    assert f"PublicKey = {RU_PUBLIC_KEY}" in de_conf
    assert f"PublicKey = {DE_PUBLIC_KEY}" in ru_conf
    assert "Private" not in prepared.stdout + (lab.tmp / "ssh.log").read_text()
    # The RU firewall admits the tunnel from DE only, and the change was confirmed.
    rules = (lab.tmp / "ru" / "input.rules").read_text()
    assert "-p udp -s 89.125.210.172 --dport 51820 -j ACCEPT" in rules
    assert "-i wg-db -p tcp -s 10.204.0.2" in rules
    assert (lab.tmp / "ru" / "input.policy").read_text().strip() == "DROP"
    assert (lab.ru_root / "etc" / "iptables" / "rules.v4").exists()
    assert "port 51820 proto udp" in (lab.tmp / "de" / "calls.log").read_text()
    # The five-year archive is sealed to the owner's key from DE's backup settings.
    if HAS_AGE:
        archive_settings = lab.ru_root / "etc" / "vpn-db" / "retention.env"
        assert f"AGE_RECIPIENT={OWNER_KEY}" in archive_settings.read_text()
        assert (
            "node-retention-archive.timer" in (lab.tmp / "ru" / "calls.log").read_text()
        )
    # Installed, not started; the bot has not noticed anything.
    assert (lab.ru_secrets / "db_gateway_token").exists()
    assert not lab.gateway_running()
    assert lab.settings() == {"BOT_USERNAME": "test_bot"}
    assert lab.bot() == "running"

    # prepare can be repeated: the firewall is not applied a second time.
    again = lab.run("prepare")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "already in place" in again.stdout

    switched = lab.run("switch")
    assert switched.returncode == 0, switched.stdout + switched.stderr
    assert "database: arrived intact" in switched.stdout
    assert "CUTOVER_DONE" in switched.stdout
    token = (lab.ru_secrets / "db_gateway_token").read_text()
    # The token is a Docker secret: its own owner-only file, and no other copy.
    assert lab.settings() == {
        "BOT_USERNAME": "test_bot",
        "DATABASE_URL": f"http://127.0.0.1:{lab.port}",
    }
    secret = lab.work / "secrets" / "db_gateway_token.txt"
    assert secret.read_text() == token
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert lab.files_holding(token) == ["secrets/db_gateway_token.txt"]
    # Nothing secret reached the phone's screen or the ssh command lines.
    shown = switched.stdout + switched.stderr + (lab.tmp / "ssh.log").read_text()
    assert token not in shown
    assert KEY not in shown
    assert (lab.ru_secrets / "db_key").read_text() == KEY
    assert (lab.ru_data / "bot.db").stat().st_size > 0
    assert not (lab.work / "data" / "move-to-ru.db").exists()
    assert lab.bot() == "running"
    assert lab.gateway_running()

    status = lab.run("status")
    assert "database: on RU, through the gateway" in status.stdout
    assert "OK gateway" in status.stdout

    # A later release: the gateway gets the new code first and keeps serving.
    updated = lab.run("update")
    assert updated.returncode == 0, updated.stdout + updated.stderr
    assert "service: restarted with the new code" in updated.stdout
    assert "CUTOVER_GATEWAY_UPDATED" in updated.stdout
    assert (lab.ru_secrets / "db_gateway_token").read_text() == token
    assert lab.gateway_running()
    twice = lab.run("switch")
    assert twice.returncode == 1
    assert "already uses the gateway" in twice.stderr

    # The connection records switch is on RU now, behind the policy check.
    gateway_settings = lab.ru_root / "etc" / "vpn-db" / "gateway.env"
    refused = lab.run("connection-log", "on", FAKE_OLD_POLICY="1")
    assert refused.returncode == 1
    assert "does not describe connection records yet" in refused.stderr
    assert "ACTIVITY_LOGGING_ENABLED=false" in gateway_settings.read_text()
    switched_on = lab.run("connection-log", "on")
    assert switched_on.returncode == 0, switched_on.stdout + switched_on.stderr
    assert "CUTOVER_CONNECTION_LOG on" in switched_on.stdout
    assert "ACTIVITY_LOGGING_ENABLED=true" in gateway_settings.read_text()
    assert "ACTIVITY_LOGGING_ENABLED" not in (lab.work / ".env").read_text()
    assert lab.run("connection-log", "off").returncode == 0
    assert "ACTIVITY_LOGGING_ENABLED=false" in gateway_settings.read_text()
    assert lab.run("connection-log", "sometimes").returncode == 1

    back = lab.run("rollback")
    assert back.returncode == 0, back.stdout + back.stderr
    assert "CUTOVER_ROLLED_BACK" in back.stdout
    assert "NOT in the file on DE" in back.stdout
    assert lab.settings() == {"BOT_USERNAME": "test_bot"}
    assert secret.read_text() == ""
    assert lab.files_holding(token) == []
    assert not lab.gateway_running()
    assert lab.bot() == "running"


def test_before_the_move_the_switch_is_the_bots_own_setting(lab: Rehearsal) -> None:
    refused = lab.run("connection-log", "on", FAKE_OLD_POLICY="1")
    assert refused.returncode == 1
    assert "ACTIVITY_LOGGING_ENABLED" not in (lab.work / ".env").read_text()
    result = lab.run("connection-log", "on")
    assert result.returncode == 0, result.stdout + result.stderr
    assert lab.settings()["ACTIVITY_LOGGING_ENABLED"] == "true"
    assert lab.run("connection-log", "off").returncode == 0
    assert lab.settings()["ACTIVITY_LOGGING_ENABLED"] == "false"


def test_without_a_backup_key_the_archive_stays_off_and_prepare_still_works(
    lab: Rehearsal,
) -> None:
    (lab.tmp / "de" / "node-backup.env").unlink()
    result = lab.run("prepare")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DE has no backup key yet" in result.stdout
    assert not (lab.ru_root / "etc" / "vpn-db" / "retention.env").exists()


def test_an_old_bot_is_updated_first(lab: Rehearsal) -> None:
    result = lab.run("prepare", FAKE_OLD_BOT="1")
    assert result.returncode == 1
    assert "update_bot.sh" in result.stderr
    assert not (lab.tmp / "ru" / "src").exists()
    assert "TOO OLD" in lab.run("check", FAKE_OLD_BOT="1").stdout


def test_switch_needs_prepare(lab: Rehearsal) -> None:
    result = lab.run("switch")
    assert result.returncode == 1
    assert "run prepare first" in result.stderr
    assert lab.bot() == "running"
    assert not (lab.ru_data / "bot.db").exists()


def test_a_file_damaged_on_the_way_switches_nothing(lab: Rehearsal) -> None:
    assert lab.run("prepare").returncode == 0
    result = lab.run("switch", FAKE_CORRUPT="1")
    assert result.returncode == 1
    assert "checksums differ" in result.stderr
    assert "Starting the bot again" in result.stdout
    assert lab.bot() == "running"
    assert lab.settings() == {"BOT_USERNAME": "test_bot"}
    assert not (lab.ru_data / "bot.db").exists()
    assert not lab.gateway_running()


def test_a_bot_that_cannot_reach_the_gateway_goes_back_by_itself(
    lab: Rehearsal,
) -> None:
    assert lab.run("prepare").returncode == 0
    result = lab.run("switch", FAKE_BOT_CANNOT_REACH="1")
    assert result.returncode == 1
    assert "back on the file on DE" in result.stderr
    assert lab.settings() == {"BOT_USERNAME": "test_bot"}
    # Nothing of the token was left behind in the bot's folder.
    token = (lab.ru_secrets / "db_gateway_token").read_text()
    assert lab.files_holding(token) == []
    assert lab.bot() == "running"
    assert not lab.gateway_running()
    # The same command works once the cause is gone.
    retry = lab.run("switch")
    assert retry.returncode == 0, retry.stdout + retry.stderr
    assert "CUTOVER_DONE" in retry.stdout


def test_servers_that_never_see_each_other_stop_prepare(lab: Rehearsal) -> None:
    result = lab.run("prepare", FAKE_NO_HANDSHAKE="1")
    assert result.returncode == 1
    assert "do not see each other" in result.stderr
    assert not (lab.ru_secrets / "db_gateway_token").exists()
    assert lab.settings() == {"BOT_USERNAME": "test_bot"}


def test_an_unreachable_server_is_reported_plainly(lab: Rehearsal) -> None:
    assert "DE does not answer" in lab.run("check", FAKE_DOWN="de").stderr
    assert "RU does not answer" in lab.run("check", FAKE_DOWN="ru").stderr
    assert lab.run("nonsense").returncode == 1
