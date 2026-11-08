"""scripts/server_update.sh with stand-ins for docker and apt: the report
changes nothing; --apply copies before it upgrades and never removes packages."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "server_update.sh"

pytestmark = pytest.mark.skipif(
    os.geteuid() != 0, reason="the script refuses to run without root"
)

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state = json.load(open(os.environ["FAKE_DOCKER_STATE"]))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("docker " + " ".join(args) + "\n")
fmt = args[args.index("--format") + 1] if "--format" in args else ""
fmt = args[args.index("-f") + 1] if "-f" in args else fmt
if args[:1] == ["version"]:
    print(state["version"])
elif args[:1] == ["ps"]:
    for c in state["containers"]:
        if "-a" not in args and not c["running"]:
            continue
        if "\\t" in fmt:
            print(f"{c['name']}\t{c['image']}")
        elif "{{.Image}}" in fmt:
            print(c["image"])
        else:
            print(c["name"])
elif args[:1] == ["inspect"]:
    match = [c for c in state["containers"] if c["name"] == args[-1]]
    if not match:
        sys.exit(1)
    running = match[0]["running"]
    if ".State.Running" in fmt:
        print("true" if running else "false")
    elif ".State.Health" in fmt:
        print("running " if running else "exited ")
    else:
        print("running" if running else "exited")
"""

OLD = "5:27.3.1-1~ubuntu.24.04~noble"
NEW = "5:28.0.1-1~ubuntu.24.04~noble"
FAKE_APT = f"""#!/usr/bin/env bash
echo "apt $*" >> "$FAKE_LOG"
echo "Listing..."
echo "openssl/noble-updates 3.0.13-0ubuntu3.6 amd64 [upgradable from: 3.0.13]"
echo "docker-ce/noble {NEW} amd64 [upgradable from: {OLD}]"
"""

FAKE_DPKG_QUERY = r"""#!/usr/bin/env bash
fmt="${2#-f=}"
shift 2
for pkg in "$@"; do
  case "$pkg" in
    docker-ce|docker-ce-cli) version="5:27.3.1-1~ubuntu.24.04~noble" ;;
    containerd.io) version="1.7.22-1" ;;
    *) continue ;;
  esac
  if [[ "$fmt" == *Package* ]]; then
    printf '%s=%s\n' "$pkg" "$version"
  else
    printf '%s' "$version"
  fi
done
"""

FAKE_APT_CACHE = """#!/usr/bin/env bash
case "$2" in
  docker-ce) echo "  Candidate: 5:28.0.1-1~ubuntu.24.04~noble" ;;
  docker-ce-cli) echo "  Candidate: 5:27.3.1-1~ubuntu.24.04~noble" ;;
  *) echo "  Candidate: 1.7.22-1" ;;
esac
"""

FAKE_DF = """#!/usr/bin/env bash
free="${FAKE_FREE_MB:-20000}"
if [[ " $* " == *" -hP "* || " $* " == *" -h "* ]]; then
  echo "Filesystem Size Used Avail Use% Mounted on"
  echo "/dev/vda1 30G 27G $((free / 1024)).0G 90% /"
else
  echo "Filesystem 1M-blocks Used Available Capacity Mounted on"
  echo "/dev/vda1 30720 $((30720 - free)) $free 90% /"
fi
"""

LOGGER = """#!/usr/bin/env bash
echo "{name} $*" >> "$FAKE_LOG"
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools = {
        "docker": FAKE_DOCKER,
        "apt": FAKE_APT,
        "dpkg-query": FAKE_DPKG_QUERY,
        "apt-cache": FAKE_APT_CACHE,
        "df": FAKE_DF,
        "apt-get": LOGGER.format(name="apt-get"),
        "ip": "#!/usr/bin/env bash\necho 'default via 10.0.0.1 dev eth0'\n",
    }
    for name, body in tools.items():
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    state = tmp_path / "docker.json"
    state.write_text(
        json.dumps(
            {
                "version": "27.3.1",
                "containers": [
                    {
                        "name": "marzban-marzban-1",
                        "image": "gozargah/marzban:v0.8.4",
                        "running": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    marzban_dir = tmp_path / "opt-marzban"
    marzban_dir.mkdir()
    (marzban_dir / ".env").write_text("UVICORN_PORT=8000\n", encoding="utf-8")
    database = tmp_path / "db.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        conn.execute("INSERT INTO users (name) VALUES ('n1')")
    # Whoever runs the tests may be on SSH or in tmux: the script reacts to both.
    session_vars = {"SSH_CONNECTION", "TMUX", "STY", "SERVER_UPDATE_DIRECT"}
    return {
        **{k: v for k, v in os.environ.items() if k not in session_vars},
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "MARZBAN_DB": str(database),
        "MARZBAN_DIR": str(marzban_dir),
    }


def _run(env: dict[str, str], *args: str) -> tuple[int, str, list[str]]:
    result = subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    log = Path(env["FAKE_LOG"])
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result.returncode, result.stdout + result.stderr, calls


def test_report_changes_nothing(env: dict[str, str]) -> None:
    code, out, calls = _run(env)
    assert code == 0, out
    assert "REPORT ONLY" in out
    assert "packages to upgrade: 2" in out
    assert "docker-ce: 5:27.3.1-1~ubuntu.24.04~noble -> 5:28.0.1" in out
    assert "marzban-marzban-1\tgozargah/marzban:v0.8.4" in out
    assert "REPORT DONE" in out
    assert not Path(env["BACKUP_DIR"]).exists()
    assert not [c for c in calls if c.startswith("apt-get") and "upgrade" in c]
    assert not [c for c in calls if c.startswith(("docker pull", "docker start"))]
    assert not [c for c in calls if "install" in c]


def test_apply_copies_first_and_never_removes_packages(env: dict[str, str]) -> None:
    backups = Path(env["BACKUP_DIR"])
    backups.mkdir()
    for n in range(4):  # older copies: only the newest three stay
        old = backups / f"marzban-db-2026010{n}T000000Z.sqlite3"
        old.write_text("old", encoding="utf-8")
        os.utime(old, (1_700_000_000 + n, 1_700_000_000 + n))

    code, out, calls = _run(env, "--apply")
    assert code == 0, out
    assert "SERVER_UPDATE_OK" in out

    copies = sorted(backups.glob("marzban-db-*.sqlite3"))
    assert len(copies) == 3
    newest = max(copies, key=lambda p: p.stat().st_mtime)
    with sqlite3.connect(newest) as conn:
        assert conn.execute("SELECT name FROM users").fetchall() == [("n1",)]
    assert oct(newest.stat().st_mode & 0o777) == "0o600"

    (settings,) = backups.glob("settings-*.tar.gz")
    with tarfile.open(settings) as archive:
        assert any(name.endswith("opt-marzban/.env") for name in archive.getnames())
    (versions,) = backups.glob("docker-versions-*.txt")
    assert "docker-ce=5:27.3.1-1~ubuntu.24.04~noble" in versions.read_text("utf-8")
    assert "apt-get install --allow-downgrades docker-ce=" in out

    upgrades = [c for c in calls if c.startswith("apt-get") and "upgrade" in c]
    assert len(upgrades) == 1
    assert "--force-confold" in upgrades[0]
    assert "--with-new-pkgs upgrade" in upgrades[0]
    assert not [c for c in calls if "dist-upgrade" in c or "autoremove" in c]
    assert not [c for c in calls if c.startswith("apt-get") and " remove" in c]
    assert "docker pull -q python:3.11-slim-bookworm" in calls
    assert "docker pull -q caddy:2.11.7" in calls
    # The Caddy compiler image stays as it is: pulling it would rebuild Caddy.
    assert "docker pull -q caddy:2.11.7-builder" not in calls
    assert "skipped: caddy:2.11.7-builder" in out
    assert "apt-get install -y -q vnstat" in calls
    assert "all 1 containers run again" in out
    assert not [c for c in calls if c.startswith("docker start")]
    # Third-party images are never pulled or changed here.
    assert not [c for c in calls if "gozargah/marzban" in c and "pull" in c]


def test_apply_restarts_a_container_left_stopped_by_the_upgrade(
    env: dict[str, str],
) -> None:
    state_path = Path(env["FAKE_DOCKER_STATE"])
    state = json.loads(state_path.read_text("utf-8"))
    # The container stops while apt runs: apt-get flips it in the state file.
    stopper = Path(env["PATH"].split(":")[0]) / "apt-get"
    stopper.write_text(
        '#!/usr/bin/env bash\necho "apt-get $*" >> "$FAKE_LOG"\n'
        'if [[ "$*" == *upgrade* ]]; then\n'
        "  python3 - \"$FAKE_DOCKER_STATE\" <<'PY'\n"
        "import json, sys\n"
        "s = json.load(open(sys.argv[1]))\n"
        "s['containers'][0]['running'] = False\n"
        "json.dump(s, open(sys.argv[1], 'w'))\n"
        "PY\nfi\n",
        encoding="utf-8",
    )
    # docker start brings it back.
    docker = Path(env["PATH"].split(":")[0]) / "docker"
    docker.write_text(
        docker.read_text("utf-8")
        + """
if args[:1] == ["start"]:
    for c in state["containers"]:
        if c["name"] == args[1]:
            c["running"] = True
    json.dump(state, open(os.environ["FAKE_DOCKER_STATE"], "w"))
""",
        encoding="utf-8",
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    code, out, calls = _run(env, "--apply")
    assert code == 0, out
    assert "docker start marzban-marzban-1" in calls
    assert "all 1 containers run again" in out


SSH = {"SSH_CONNECTION": "203.0.113.7 51000 10.0.0.5 22"}


def test_apply_over_ssh_asks_for_tmux_and_starts_nothing(env: dict[str, str]) -> None:
    code, out, calls = _run({**env, **SSH}, "--apply")
    assert code == 2
    assert "SERVER_UPDATE_NOT_STARTED" in out
    assert "tmux new -s update" in out
    assert "tmux attach -t update" in out
    assert calls == []  # not a single docker or apt call
    assert not Path(env["BACKUP_DIR"]).exists()


@pytest.mark.parametrize(
    "session",
    [
        {"TMUX": "tmux-socket,1,0"},
        {"STY": "123.pts-0.host"},
        {"SERVER_UPDATE_DIRECT": "1"},
    ],
    ids=["tmux", "screen", "override"],
)
def test_apply_over_ssh_runs_where_a_dropped_link_cannot_stop_it(
    env: dict[str, str], session: dict[str, str]
) -> None:
    code, out, _ = _run({**env, **SSH, **session}, "--apply")
    assert code == 0, out
    assert "SERVER_UPDATE_OK" in out


def test_the_report_needs_no_session_over_ssh(env: dict[str, str]) -> None:
    code, out, _ = _run({**env, **SSH})
    assert code == 0, out
    assert "REPORT DONE" in out


def test_report_says_when_the_disk_is_tight(env: dict[str, str]) -> None:
    code, out, calls = _run({**env, "FAKE_FREE_MB": "3000"})
    assert code == 0, out
    assert "3000 MB free, less than 5 GB wanted" in out
    assert "stops below 3 GB" in out
    assert not [c for c in calls if "upgrade" in c]


def test_apply_does_not_start_when_the_cleanup_cannot_free_enough(
    env: dict[str, str],
) -> None:
    code, out, calls = _run({**env, "FAKE_FREE_MB": "2500"}, "--apply")
    assert code == 1
    assert "SERVER_UPDATE_STOPPED: only 2500 MB free" in out
    assert not Path(env["BACKUP_DIR"]).exists()
    assert not [c for c in calls if c.startswith(("apt-get", "docker pull"))]


def test_apply_goes_on_when_the_space_is_tight_but_enough(
    env: dict[str, str],
) -> None:
    code, out, _ = _run({**env, "FAKE_FREE_MB": "4000"}, "--apply")
    assert code == 0, out
    assert "4000 MB free, less than 5 GB wanted: cleaning up first" in out
    assert "after the cleanup: 4000 MB free" in out
    assert "SERVER_UPDATE_OK" in out
