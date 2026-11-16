"""The second kind of VLESS entry: the candidate config, the users, the switch."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.hiddify import HiddifyClient

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vless_transport.sh"
KEY = "PRIVATE-KEY-VALUE"


def _load(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


xray_add = _load("xray_add_inbound")
entries = _load("vpn_entries")

BASE_CONFIG: dict[str, Any] = {
    "inbounds": [
        {
            "tag": "VLESS TCP REALITY",
            "listen": "0.0.0.0",  # noqa: S104
            "port": 2096,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "dest": "www.apple.com:443",
                    "serverNames": ["www.apple.com"],
                    "privateKey": KEY,
                    "shortIds": ["ab12"],
                },
            },
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        }
    ],
    "outbounds": [{"protocol": "freedom", "tag": "DIRECT"}],
}


def test_the_new_inbound_borrows_the_key_and_has_its_own_cover_and_port() -> None:
    result = xray_add.add_inbound(
        BASE_CONFIG,
        base="VLESS TCP REALITY",
        tag="VLESS-GRPC-REALITY",
        cover="dl.example.com",
        port=2097,
    )
    old, new = result["inbounds"]
    assert old == BASE_CONFIG["inbounds"][0]
    assert new["tag"] == "VLESS-GRPC-REALITY"
    assert new["port"] == 2097
    stream = new["streamSettings"]
    assert stream["network"] == "grpc"
    assert len(stream["grpcSettings"]["serviceName"]) == 12
    assert stream["security"] == "reality"
    reality = stream["realitySettings"]
    # Without a key of its own the base's is borrowed; the short id is always new.
    assert reality["privateKey"] == KEY
    assert reality["shortIds"] != ["ab12"]
    assert len(reality["shortIds"][0]) == 16
    assert reality["serverNames"] == ["dl.example.com"]
    assert reality["dest"] == "dl.example.com:443"
    assert new["sniffing"] == BASE_CONFIG["inbounds"][0]["sniffing"]
    assert result["outbounds"] == BASE_CONFIG["outbounds"]
    assert len(BASE_CONFIG["inbounds"]) == 1  # the input is untouched


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"cover": "not a name"}, "not a host name"),
        ({"cover": "localhost"}, "not a host name"),
        ({"tag": "VLESS TCP REALITY"}, "no spaces"),
        ({"tag": "VLESS-TCP"}, None),
        ({"port": 80}, "1024-65535"),
        ({"port": 2096}, "already used"),
        ({"base": "nothing"}, "no inbound"),
    ],
)
def test_bad_requests_are_refused(changes: dict[str, Any], message: str | None) -> None:
    args: dict[str, Any] = {
        "base": "VLESS TCP REALITY",
        "tag": "VLESS-GRPC-REALITY",
        "cover": "dl.example.com",
        "port": 2097,
    } | changes
    if message is None:
        assert xray_add.add_inbound(BASE_CONFIG, **args)
        return
    with pytest.raises(ValueError, match=message):
        xray_add.add_inbound(BASE_CONFIG, **args)


OWN_KEY = "k" * 43


def test_an_own_key_and_a_tcp_entry() -> None:
    result = xray_add.add_inbound(
        BASE_CONFIG,
        base="VLESS TCP REALITY",
        tag="VLESS-TCP-REALITY-RU",
        cover="ya.example.ru",
        port=2098,
        network="tcp",
        private_key=OWN_KEY,
    )
    new = result["inbounds"][1]
    stream = new["streamSettings"]
    assert stream["network"] == "tcp"
    assert "grpcSettings" not in stream
    assert stream["realitySettings"]["privateKey"] == OWN_KEY
    with pytest.raises(ValueError, match="x25519"):
        xray_add.add_inbound(
            BASE_CONFIG,
            base="VLESS TCP REALITY",
            tag="X-1-2",
            cover="a.example.com",
            port=2099,
            private_key="short",
        )
    xhttp = xray_add.add_inbound(
        BASE_CONFIG,
        base="VLESS TCP REALITY",
        tag="VLESS-XHTTP-REALITY",
        cover="a.example.com",
        port=2099,
        network="xhttp",
    )["inbounds"][1]["streamSettings"]
    assert xhttp["network"] == "xhttp"
    assert xhttp["xhttpSettings"]["mode"] == "auto"
    assert xhttp["xhttpSettings"]["path"].startswith("/")
    assert len(xhttp["xhttpSettings"]["path"]) == 13
    with pytest.raises(ValueError, match="grpc, tcp or xhttp"):
        xray_add.add_inbound(
            BASE_CONFIG,
            base="VLESS TCP REALITY",
            tag="X-1-2",
            cover="a.example.com",
            port=2099,
            network="ws",
        )


def test_the_same_tag_twice_is_refused() -> None:
    once = xray_add.add_inbound(
        BASE_CONFIG,
        base="VLESS TCP REALITY",
        tag="VLESS-GRPC-REALITY",
        cover="dl.example.com",
        port=2097,
    )
    with pytest.raises(ValueError, match="already there"):
        xray_add.add_inbound(
            once,
            base="VLESS TCP REALITY",
            tag="VLESS-GRPC-REALITY",
            cover="dl.example.com",
            port=2098,
        )


def test_the_cli_writes_a_candidate_and_prints_no_secret(tmp_path: Path) -> None:
    config = tmp_path / "xray.json"
    config.write_text(json.dumps(BASE_CONFIG))
    candidate = tmp_path / "candidate.json"
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts" / "xray_add_inbound.py"),
            str(config),
            str(candidate),
            "--cover",
            "dl.example.com",
            "--port",
            "2097",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout
    assert "XRAY_CANDIDATE_OK" in done.stdout
    assert KEY not in done.stdout
    assert len(json.loads(candidate.read_text())["inbounds"]) == 2
    assert json.loads(config.read_text()) == BASE_CONFIG


def test_users_get_the_tag_once() -> None:
    base = "VLESS TCP REALITY"
    assert entries.tags_with([base], base, "VLESS-GRPC-REALITY") == [
        base,
        "VLESS-GRPC-REALITY",
    ]
    assert entries.tags_with([], base, "X-1") == [base, "X-1"]
    assert entries.tags_with([base, "X-1"], base, "X-1") is None


def test_hiddify_client_has_no_per_user_inbound_assignment() -> None:
    """Hiddify Manager applies proxies globally; there is no per-user inbound API."""
    assert not hasattr(HiddifyClient, "set_vless_inbounds")
    assert not hasattr(HiddifyClient, "extra_inbound_tags")

# ----------------------------------------------------------- the shell switch
DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$T/calls.log"
case "$1" in
  inspect) exit 0 ;;
  restart) [ -f "$T/restart_breaks" ] || touch "$T/listen_new"; exit 0 ;;
  exec)
    shift
    args="$*"
    case "$args" in
      *"run -test"*) [ -f "$T/xray_refuses" ] && exit 1; exit 0 ;;
      *"x25519"*)
        echo "Private key: OWNKEYOWNKEYOWNKEYOWNKEYOWNKEYOWNKEYOWNKEYO"
        echo "Public key: PUB" ;;
      *"printenv HIDDIFY_EXTRA_INBOUND_TAGS"*) cat "$T/extra_tags" 2>/dev/null ;;
      *"--attach"*) echo "VPN_ENTRIES_ATTACHED: 3 users" ;;
    esac
    ;;
esac
"""
SS = r"""#!/bin/bash
echo "State Recv-Q Send-Q Local Peer"
echo "LISTEN 0 0 0.0.0.0:2096 0.0.0.0:*"
if [ -f "$T/listen_new" ]; then
  echo "LISTEN 0 0 0.0.0.0:2097 0.0.0.0:*"
  echo "LISTEN 0 0 0.0.0.0:2098 0.0.0.0:*"
fi
"""
OPENSSL = (
    '#!/bin/bash\necho "openssl $*" >> "$T/calls.log"\n'
    'if [ -f "$T/no_x25519" ]; then echo "Server Temp Key: ECDH, P-256"; '
    'else echo "Server Temp Key: X25519, 253 bits"; fi\n'
)
CURL = '#!/bin/bash\necho "curl $*" >> "$T/calls.log"\ncat "$T/tls_answer"\n'
UFW = '#!/bin/bash\necho "ufw $*" >> "$T/calls.log"\n'
UPDATE_BOT = '#!/bin/bash\necho "update_bot $*" >> "$T/calls.log"\n'

def switch(
    tmp_path: Path, *args: str, **extra: str
) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in {
        "docker": DOCKER,
        "ss": SS,
        "ufw": UFW,
        "curl": CURL,
        "openssl": OPENSSL,
    }.items():
        (bindir / name).write_text(body)
        (bindir / name).chmod(0o755)
    scripts = tmp_path / "src" / "scripts"
    if not scripts.exists():
        scripts.mkdir(parents=True)
        for name in ("vless_transport.sh", "xray_add_inbound.py", "vpn_entries.py"):
            (scripts / name).write_text((ROOT / "scripts" / name).read_text())
        (scripts / "update_bot.sh").write_text(UPDATE_BOT)
    env = {
        **os.environ,
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "T": str(tmp_path),
        "XRAY_CONFIG": str(tmp_path / "xray_config.json"),
        "TRANSPORT_STATE": str(tmp_path / "state"),
        "WAIT_SECONDS": "4",
        **extra,
    }
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(scripts / "vless_transport.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.fixture
def server(tmp_path: Path) -> Path:
    (tmp_path / "xray_config.json").write_text(json.dumps(BASE_CONFIG))
    (tmp_path / "tls_answer").write_text("2")
    return tmp_path


def calls(tmp_path: Path) -> str:
    path = tmp_path / "calls.log"
    return path.read_text() if path.exists() else ""


def test_check_changes_nothing(server: Path) -> None:
    done = switch(server, "--check", "dl.example.com")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "VLESS_TRANSPORT_CHECK_OK" in done.stdout
    assert json.loads((server / "xray_config.json").read_text()) == BASE_CONFIG
    assert "restart" not in calls(server)
    assert "ufw" not in calls(server)
    assert KEY not in done.stdout + done.stderr


def test_a_cover_without_tls_13_is_refused(server: Path) -> None:
    (server / "tls_answer").write_text("1.1")
    done = switch(server, "--apply", "old.example.com")
    assert done.returncode == 1
    assert "does not answer TLS 1.3 with HTTP/2" in done.stderr
    assert "--tlsv1.3 --http2" in calls(server)
    assert json.loads((server / "xray_config.json").read_text()) == BASE_CONFIG


def test_a_config_xray_refuses_is_never_used(server: Path) -> None:
    (server / "xray_refuses").touch()
    done = switch(server, "--apply", "dl.example.com")
    assert done.returncode == 1
    assert "nothing was changed" in done.stderr
    assert json.loads((server / "xray_config.json").read_text()) == BASE_CONFIG
    assert "restart" not in calls(server)


def test_apply_switches_and_rollback_undoes(server: Path) -> None:
    done = switch(server, "--apply", "dl.example.com", "2097")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "VLESS_TRANSPORT_OK: VLESS-GRPC-REALITY (grpc) on 2097" in done.stdout
    live = json.loads((server / "xray_config.json").read_text())
    assert [i["port"] for i in live["inbounds"]] == [2096, 2097]
    # Its own key from xray x25519, never printed.
    own = live["inbounds"][1]["streamSettings"]["realitySettings"]["privateKey"]
    assert own == "OWNKEYOWNKEYOWNKEYOWNKEYOWNKEYOWNKEYOWNKEYO"
    assert own not in done.stdout + done.stderr
    log = calls(server)
    assert "docker restart hiddify-manager" in log
    assert "ufw allow 2097/tcp comment VLESS VLESS-GRPC-REALITY" in log
    assert "--attach VLESS-GRPC-REALITY" in log
    assert "update_bot --set HIDDIFY_EXTRA_INBOUND_TAGS=VLESS-GRPC-REALITY" in log
    back = switch(server, "--rollback")
    assert back.returncode == 0, back.stdout + back.stderr
    assert "VLESS_TRANSPORT_ROLLED_BACK" in back.stdout
    assert json.loads((server / "xray_config.json").read_text()) == BASE_CONFIG
    assert "ufw delete allow 2097/tcp" in calls(server)
    assert "update_bot --set HIDDIFY_EXTRA_INBOUND_TAGS=" in calls(server)


def test_xray_that_does_not_come_back_is_rolled_back_at_once(server: Path) -> None:
    # The new port never starts listening after the restart.
    (server / "restart_breaks").touch()
    done = switch(server, "--apply", "dl.example.com", "2097")
    assert done.returncode == 1
    assert "the previous config is back" in done.stderr
    assert json.loads((server / "xray_config.json").read_text()) == BASE_CONFIG
    assert "--attach" not in calls(server)
    assert "ufw allow" not in calls(server)


def test_a_relay_only_tcp_entry_keeps_the_other_tags(server: Path) -> None:
    (server / "extra_tags").write_text("VLESS-GRPC-REALITY")
    done = switch(
        server,
        "--apply",
        "ya.example.ru",
        "2098",
        TRANSPORT_TAG="VLESS-TCP-REALITY-RU",
        TRANSPORT_NETWORK="tcp",
        ALLOW_FROM="198.51.100.7",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "VLESS-TCP-REALITY-RU (tcp) on 2098" in done.stdout
    log = calls(server)
    assert "ufw allow from 198.51.100.7 to any port 2098 proto tcp" in log
    assert (
        "update_bot --set HIDDIFY_EXTRA_INBOUND_TAGS="
        "VLESS-GRPC-REALITY,VLESS-TCP-REALITY-RU" in log
    )
    assert "TRANSPORT_TAG=VLESS-TCP-REALITY-RU ALLOW_FROM=198.51.100.7" in done.stdout
    back = switch(
        server,
        "--rollback",
        TRANSPORT_TAG="VLESS-TCP-REALITY-RU",
        ALLOW_FROM="198.51.100.7",
    )
    assert back.returncode == 0, back.stdout + back.stderr
    assert "update_bot --set HIDDIFY_EXTRA_INBOUND_TAGS=VLESS-GRPC-REALITY\n" in (
        calls(server)
    )


def test_a_cover_without_x25519_is_refused(server: Path) -> None:
    (server / "no_x25519").touch()
    done = switch(server, "--check", "dl.example.com")
    assert done.returncode == 1
    assert "X25519" in done.stderr
