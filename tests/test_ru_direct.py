"""Russian sites past the server, handed to the apps inside the subscription.

The templates are Marzban's own defaults plus the rules (scripts/
ru_direct_templates.py); the server script (scripts/ru_direct.sh) runs for real
with stand-ins for docker, the marzban command, curl and the bot's updater. What
is checked is what keeps the panel safe: Xray must accept the config before
anything changes, an owner's own template is never replaced, and a panel that
does not answer after the restart gets the previous settings back."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ru_direct_templates as templates  # noqa: E402

# Shaped like Marzban 0.8.4's app/templates/v2ray/default.json, which listens on
# every interface: the template made from it must not.
ANY = "0.0.0.0"  # noqa: S104
V2RAY_DEFAULT: dict[str, Any] = {
    "log": {"access": "", "error": "", "loglevel": "warning"},
    "inbounds": [
        {
            "tag": "socks",
            "port": 10808,
            "listen": ANY,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "sniffing": {"enabled": True, "destOverride": ["http", "tls"]},
        },
        {
            "tag": "http",
            "port": 10809,
            "listen": ANY,
            "protocol": "http",
            "settings": {"allowTransparent": False},
        },
    ],
    "outbounds": [],
    "dns": {"servers": ["1.1.1.1", "8.8.8.8"]},
    "routing": {"domainStrategy": "AsIs", "rules": []},
}
# Shaped like Marzban 0.8.4's app/templates/singbox/default.json.
SINGBOX_DEFAULT: dict[str, Any] = {
    "log": {"level": "warn"},
    "dns": {
        "servers": [
            {"tag": "dns-remote", "address": "1.1.1.2", "detour": "proxy"},
            {"tag": "dns-local", "address": "local", "detour": "direct"},
        ],
        "rules": [{"outbound": "any", "server": "dns-local"}],
        "final": "dns-remote",
    },
    "inbounds": [{"type": "tun", "tag": "tun-in", "auto_route": True}],
    "outbounds": [
        {"type": "selector", "tag": "proxy", "outbounds": []},
        {"type": "urltest", "tag": "Best Latency", "outbounds": []},
        {"type": "direct", "tag": "direct"},
        {"type": "block", "tag": "block"},
        {"type": "dns", "tag": "dns-out"},
    ],
    "route": {
        "rules": [
            {"protocol": "dns", "outbound": "dns-out"},
            {"ip_is_private": True, "outbound": "direct"},
        ],
        "final": "proxy",
    },
}


# ------------------------------------------------------------ the templates
def test_v2ray_template_sends_russian_names_and_addresses_direct() -> None:
    result = templates.v2ray_template(V2RAY_DEFAULT)
    rules = result["routing"]["rules"]
    assert {"domain": templates.RU_DOMAINS, "outboundTag": "direct"}.items() <= rules[
        1
    ].items()
    assert rules[2]["ip"] == ["geoip:ru"]
    assert rules[0]["ip"] == ["geoip:private"]
    # A name that matches no rule is looked up, so a Russian address goes direct.
    assert result["routing"]["domainStrategy"] == "IPIfNonMatch"
    tags = [o["tag"] for o in result["outbounds"]]
    assert tags == ["direct", "block"]  # Marzban puts the server before these
    assert result["dns"]["servers"][0]["address"] == templates.RU_DNS
    assert result["dns"]["servers"][1:] == ["1.1.1.1", "8.8.8.8"]
    # The local ports are for the app on the device only, not for the Wi-Fi.
    assert {i["listen"] for i in result["inbounds"]} == {"127.0.0.1"}
    assert V2RAY_DEFAULT["inbounds"][0]["listen"] == ANY  # input untouched


def test_the_rules_need_no_geosite_list() -> None:
    # A geosite tag missing from an older app's file stops its core from starting.
    for made in (
        templates.v2ray_template(V2RAY_DEFAULT),
        templates.singbox_template(SINGBOX_DEFAULT),
    ):
        assert "geosite" not in json.dumps(made)


def test_running_twice_adds_nothing() -> None:
    once = templates.v2ray_template(V2RAY_DEFAULT)
    assert templates.v2ray_template(once) == once
    box = templates.singbox_template(SINGBOX_DEFAULT)
    assert templates.singbox_template(box) == box


def test_singbox_template_puts_the_rule_before_the_server_and_asks_local_dns() -> None:
    result = templates.singbox_template(SINGBOX_DEFAULT)
    rules = result["route"]["rules"]
    assert rules[0] == {"protocol": "dns", "outbound": "dns-out"}
    assert rules[1] == {"ip_is_private": True, "outbound": "direct"}
    assert rules[2] == {"domain_suffix": templates.RU_SUFFIXES, "outbound": "direct"}
    assert result["route"]["final"] == "proxy"
    assert result["dns"]["rules"][0] == {
        "domain_suffix": templates.RU_SUFFIXES,
        "server": "dns-local",
    }
    assert result["outbounds"] == SINGBOX_DEFAULT["outbounds"]


def test_check_config_puts_a_stand_in_server_first() -> None:
    config = templates.check_config(templates.v2ray_template(V2RAY_DEFAULT))
    assert [o["tag"] for o in config["outbounds"]] == ["proxy", "direct", "block"]
    assert config["outbounds"][0]["settings"]["vnext"][0]["address"] == "127.0.0.1"


def test_command_line(tmp_path: Path) -> None:
    def run(kind: str, data: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            [sys.executable, str(SCRIPTS / "ru_direct_templates.py"), kind],
            input=data,
            capture_output=True,
            text=True,
            check=False,
        )

    made = run("v2ray", json.dumps(V2RAY_DEFAULT))
    assert made.returncode == 0
    assert json.loads(made.stdout) == templates.v2ray_template(V2RAY_DEFAULT)
    assert run("v2ray", "not json").returncode == 1
    assert run("v2ray", "[1, 2]").returncode == 1
    assert run("clash", "{}").returncode == 2


# ------------------------------------------------------ the server script
FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
state_path = os.environ["FAKE_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("docker " + " ".join(args) + "\n")
if args[:1] == ["inspect"]:
    print("true" if state.get("running", True) else "false")
    sys.exit(0)
if args[:1] == ["exec"]:
    rest = args[1:]
    env = {}
    while rest and rest[0] == "-e":
        key, _, value = rest[1].partition("=")
        env[key] = value
        rest = rest[2:]
    rest = rest[1:]  # the container
    if rest[0] == "cat":
        name = os.path.basename(os.path.dirname(rest[1]))
        print(json.dumps(state["defaults"][name]))
    elif rest[0] == "test":
        sys.exit(0 if state.get("mounted", True) and os.path.exists(rest[2]) else 1)
    elif rest[0] == "printenv":
        print(state.get("env", {}).get(rest[1], ""))
    elif rest[1:3] == ["run", "-test"]:
        state["xray_assets"] = env.get("XRAY_LOCATION_ASSET")
        json.dump(state, open(state_path, "w"))
        config = json.load(open(rest[-1]))
        ok = state.get("xray_ok", True) and config["outbounds"][0]["tag"] == "proxy"
        print("Configuration OK." if ok else "Failed to start: geoip:xx not found")
    sys.exit(0)
sys.exit(0)
"""

# "marzban restart -n": the panel reads .env again when it comes back.
FAKE_MARZBAN = r"""#!/usr/bin/env python3
import json, os, re, sys
state_path = os.environ["FAKE_STATE"]
state = json.load(open(state_path))
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write("marzban " + " ".join(sys.argv[1:]) + "\n")
env = {}
for line in open(os.environ["MARZBAN_DIR"] + "/.env"):
    m = re.match(r'^\s*([A-Z0-9_]+)\s*=\s*"?([^"\n]*)"?\s*$', line)
    if m:
        env[m.group(1)] = m.group(2)
state["env"] = env
state["restarts"] = state.get("restarts", 0) + 1
template = env.get("V2RAY_SUBSCRIPTION_TEMPLATE")
broken = state.get("breaks_on") and template == state["breaks_on"]
state["up"] = not broken
json.dump(state, open(state_path, "w"))
"""

FAKE_CURL = r"""#!/usr/bin/env python3
import json, os
state = json.load(open(os.environ["FAKE_STATE"]))
print("401" if state.get("up", True) else "000", end="")
"""

FAKE_BOT_UPDATER = r"""#!/usr/bin/env bash
echo "update_bot $*" >> "$FAKE_LOG"
"""


def _stub(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def server(tmp_path: Path) -> dict[str, Any]:
    marzban = tmp_path / "opt-marzban"
    data = tmp_path / "var-lib-marzban"
    marzban.mkdir()
    data.mkdir()
    env_file = marzban / ".env"
    env_file.write_text(
        'UVICORN_PORT = 8000\nXRAY_ASSETS_PATH = "/var/lib/marzban/assets"\n'
        '# V2RAY_SUBSCRIPTION_TEMPLATE="v2ray/default.json"\n'
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir, "docker", FAKE_DOCKER)
    _stub(bin_dir, "marzban", FAKE_MARZBAN)
    _stub(bin_dir, "curl", FAKE_CURL)
    _stub(tmp_path, "update_bot.sh", FAKE_BOT_UPDATER)
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps({"defaults": {"v2ray": V2RAY_DEFAULT, "singbox": SINGBOX_DEFAULT}})
    )
    log = tmp_path / "log"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MARZBAN_DIR": str(marzban),
        "MARZBAN_DATA": str(data),
        "BOT_UPDATER": str(tmp_path / "update_bot.sh"),
        "WAIT_SECONDS": "2",
        "WAIT_STEP_SECONDS": "1",
        "FAKE_STATE": str(state),
        "FAKE_LOG": str(log),
    }
    return {"env": env, "env_file": env_file, "data": data, "state": state, "log": log}


def _run(server: dict[str, Any], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPTS / "ru_direct.sh"), *args],  # noqa: S607
        env=server["env"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _state(server: dict[str, Any]) -> dict[str, Any]:
    return json.loads(server["state"].read_text())


def _change(server: dict[str, Any], **changes: Any) -> None:
    state = _state(server)
    state.update(changes)
    server["state"].write_text(json.dumps(state))


def test_the_report_changes_nothing(server: dict[str, Any]) -> None:
    before = server["env_file"].read_text()
    result = _run(server)
    assert result.returncode == 0
    assert "RU_DIRECT_STATE off" in result.stdout
    assert server["env_file"].read_text() == before
    assert server["log"].read_text() == ""


def test_apply_checks_with_xray_then_turns_it_on(server: dict[str, Any]) -> None:
    result = _run(server, "--apply")
    assert result.returncode == 0, result.stderr
    assert "Configuration OK" in result.stdout
    assert "RU_DIRECT_OK" in result.stdout
    text = server["env_file"].read_text()
    assert 'V2RAY_SUBSCRIPTION_TEMPLATE="v2ray/ru-direct.json"' in text
    assert 'SINGBOX_SUBSCRIPTION_TEMPLATE="singbox/ru-direct.json"' in text
    assert 'USE_CUSTOM_JSON_FOR_V2RAYNG="True"' in text
    assert f'CUSTOM_TEMPLATES_DIRECTORY="{server["data"]}/templates/"' in text
    assert "UVICORN_PORT = 8000" in text  # the rest is kept
    made = json.loads((server["data"] / "templates/v2ray/ru-direct.json").read_text())
    assert made == templates.v2ray_template(V2RAY_DEFAULT)
    box = json.loads((server["data"] / "templates/singbox/ru-direct.json").read_text())
    assert box == templates.singbox_template(SINGBOX_DEFAULT)
    assert not (server["data"] / "templates/.ru-direct-check.json").exists()
    # The geo files the panel's own Xray uses.
    assert _state(server)["xray_assets"] == "/var/lib/marzban/assets"
    log = server["log"].read_text()
    assert "marzban restart -n" in log
    assert "update_bot --set RU_DIRECT_SUBSCRIPTION=true" in log
    assert list(server["env_file"].parent.glob(".env.before-ru-direct-*"))
    assert "RU_DIRECT_STATE on" in _run(server).stdout


def test_a_config_xray_rejects_changes_nothing(server: dict[str, Any]) -> None:
    _change(server, xray_ok=False)
    before = server["env_file"].read_text()
    result = _run(server, "--apply")
    assert result.returncode == 1
    assert "Xray rejected the config; nothing was changed" in result.stderr
    assert server["env_file"].read_text() == before
    assert not (server["data"] / "templates/v2ray/ru-direct.json").exists()
    assert "marzban restart" not in server["log"].read_text()


def test_a_panel_that_does_not_answer_gets_the_old_settings_back(
    server: dict[str, Any],
) -> None:
    _change(server, breaks_on="v2ray/ru-direct.json")
    before = server["env_file"].read_text()
    result = _run(server, "--apply")
    assert result.returncode == 1
    assert "the previous settings are back" in result.stderr
    assert server["env_file"].read_text() == before
    assert _state(server)["restarts"] == 2  # the change, then the way back
    assert "RU_DIRECT_SUBSCRIPTION=true" not in server["log"].read_text()


def test_an_owners_own_template_is_not_replaced(server: dict[str, Any]) -> None:
    with server["env_file"].open("a") as env:
        env.write('V2RAY_SUBSCRIPTION_TEMPLATE = "v2ray/mine.json"\n')
    before = server["env_file"].read_text()
    result = _run(server, "--apply")
    assert result.returncode == 1
    assert "your own template" in result.stderr
    assert server["env_file"].read_text() == before


def test_a_templates_folder_the_container_does_not_see_is_refused(
    server: dict[str, Any],
) -> None:
    _change(server, mounted=False)
    before = server["env_file"].read_text()
    result = _run(server, "--apply")
    assert result.returncode == 1
    assert "does not see" in result.stderr
    assert server["env_file"].read_text() == before


def test_when_on_a_rerun_refreshes_the_templates_without_a_restart(
    server: dict[str, Any],
) -> None:
    assert _run(server, "--apply").returncode == 0
    restarts = _state(server)["restarts"]
    result = _run(server, "--apply")
    assert result.returncode == 0
    assert "already on" in result.stdout
    assert _state(server)["restarts"] == restarts


def test_remove_turns_it_off_and_tells_the_bot(server: dict[str, Any]) -> None:
    assert _run(server, "--apply").returncode == 0
    result = _run(server, "--remove")
    assert result.returncode == 0, result.stderr
    assert "RU_DIRECT_REMOVED" in result.stdout
    text = server["env_file"].read_text()
    assert "V2RAY_SUBSCRIPTION_TEMPLATE=" not in text.replace("# V2RAY", "")
    assert "USE_CUSTOM_JSON_FOR_V2RAYNG" not in text
    assert "update_bot --set RU_DIRECT_SUBSCRIPTION=false" in server["log"].read_text()
    assert "RU_DIRECT_STATE off" in _run(server).stdout


def test_bad_arguments(server: dict[str, Any]) -> None:
    assert _run(server, "--now").returncode == 1
    assert _run(server, "--apply", "--remove").returncode == 1
