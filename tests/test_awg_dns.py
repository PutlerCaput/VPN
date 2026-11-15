"""AmneziaWG DNS filter: block-zone builder, resolver config and tunnel firewall."""

from __future__ import annotations

import importlib.util
import logging
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


blocklist = _load("awg_blocklist", ROOT / "deploy" / "awg" / "dns" / "blocklist.py")
agent_module = _load("awg_agent_dns", ROOT / "deploy" / "awg" / "agent.py")


# ----------------------------------------------------------- block zone
def test_plain_hosts_and_comment_lines_give_one_domain_each() -> None:
    lines = [
        "example.com\n",
        "0.0.0.0 ads.example.net  # comment\n",
        "# just a comment\n",
        "\n",
        "*.wild.example.org\n",
        "Mixed.Case.Example.COM.\n",
    ]
    assert blocklist.parse(lines) == {
        "example.com",
        "ads.example.net",
        "wild.example.org",
        "mixed.case.example.com",
    }


def test_invalid_names_and_localhost_are_skipped() -> None:
    lines = [
        "localhost\n",
        "not a domain!\n",
        "192.168.1.1\n",
        "a..b.com\n",
        "-x.com\n",
    ]
    assert blocklist.parse(lines) == set()


def test_render_names_zones_with_always_nxdomain_in_sorted_order() -> None:
    text = blocklist.render({"b.example", "a.example"})
    zones = [line for line in text.splitlines() if line.startswith("local-zone")]
    assert zones == [
        'local-zone: "a.example" always_nxdomain',
        'local-zone: "b.example" always_nxdomain',
    ]


def test_too_few_domains_is_refused() -> None:
    with pytest.raises(ValueError, match="truncated"):
        blocklist.check_count(blocklist.MIN_DOMAINS - 1)


def test_domain_cap_matches_the_zone_guard() -> None:
    assert blocklist.MAX_DOMAINS == 1_000_000
    assert agent_module.MAX_ZONE_ENTRIES == blocklist.MAX_DOMAINS


def test_a_failed_build_keeps_the_previous_file(tmp_path: Path) -> None:
    out = tmp_path / "blocklist.conf"
    out.write_text("previous\n", encoding="utf-8")
    source = tmp_path / "short.txt"
    source.write_text("one.example\n", encoding="utf-8")
    assert blocklist.main(["--out", str(out), str(source)]) == 1
    assert out.read_text(encoding="utf-8") == "previous\n"


def test_a_good_build_replaces_the_file(tmp_path: Path) -> None:
    out = tmp_path / "blocklist.conf"
    source = tmp_path / "big.txt"
    source.write_text(
        "".join(f"site{n}.example\n" for n in range(blocklist.MIN_DOMAINS)),
        encoding="utf-8",
    )
    assert blocklist.main(["--out", str(out), str(source)]) == 0
    text = out.read_text(encoding="utf-8")
    assert text.count("always_nxdomain") == blocklist.MIN_DOMAINS
    assert not (tmp_path / "blocklist.conf.tmp").exists()


# ------------------------------------------------------- resolver config
def test_resolver_answers_tunnel_clients_only() -> None:
    conf = agent_module.render_resolver_conf(
        "10.67.0.1", "10.67.0.0/24", Path("/var/lib/awg/dns/blocklist.conf")
    )
    assert "interface: 10.67.0.1" in conf
    assert "access-control: 10.67.0.0/24 allow" in conf
    assert "0.0.0.0/0" not in conf
    assert 'include: "/var/lib/awg/dns/blocklist.conf"' in conf
    assert "forward-tls-upstream: yes" in conf
    assert "@853#cloudflare-dns.com" in conf
    assert 'username: ""' in conf
    assert 'chroot: ""' in conf
    assert 'directory: "/var/lib/awg/dns"' in conf
    assert 'pidfile: "/var/lib/awg/dns/unbound.pid"' in conf
    assert "num-threads: 1" in conf
    assert "msg-cache-size: 8m" in conf
    assert "rrset-cache-size: 16m" in conf


# ------------------------------------------------------------ the agent
class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args: Any, stdin: str | None = None) -> str:
        args = list(args)
        self.calls.append(args)
        if args[1:] == ["genkey"]:
            return "A" * 43 + "=\n"
        if args[1:] == ["pubkey"]:
            return "B" * 43 + "=\n"
        return ""


def make_agent(tmp_path: Path, dns_filter: bool) -> Any:
    config = agent_module.Config(
        subnet="10.67.0.0/24",
        endpoint_host="203.0.113.5",
        state_dir=tmp_path / "state",
        run_dir=tmp_path,
        token="t" * 40,
        dns_filter=dns_filter,
    )
    return agent_module.Agent(config, FakeRunner())


def iptables_rules(agent: Any) -> list[str]:
    return [" ".join(c[1:]) for c in agent.run.calls if c[0].endswith("iptables")]


def test_clients_are_told_to_use_the_server_resolver(tmp_path: Path) -> None:
    assert make_agent(tmp_path, True).client_dns == "10.67.0.1"
    assert make_agent(tmp_path, False).client_dns == "1.1.1.1, 1.0.0.1"


def test_filter_lets_clients_reach_only_the_resolver(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, True)
    agent._firewall("awg0")
    rules = iptables_rules(agent)
    accept_dns = rules.index("-A INPUT -i awg0 -p udp --dport 53 -j ACCEPT")
    assert accept_dns < rules.index("-A INPUT -i awg0 -j DROP")
    assert "-A INPUT -i awg0 -p tcp --dport 53 -j ACCEPT" in rules


def test_filter_redirects_plain_dns_to_the_resolver(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, True)
    agent._firewall("awg0")
    rules = iptables_rules(agent)
    for proto in ("udp", "tcp"):
        assert (
            f"-t nat -A PREROUTING -i awg0 -p {proto} --dport 53 "
            "-j DNAT --to-destination 10.67.0.1:53"
        ) in rules


def test_filter_refuses_other_resolvers_before_the_internet_accept(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path, True)
    agent._firewall("awg0")
    rules = iptables_rules(agent)
    for rule in (
        "-A FORWARD -i awg0 -p udp --dport 53 -j DROP",
        "-A FORWARD -i awg0 -p tcp --dport 53 -j DROP",
        "-A FORWARD -i awg0 -p tcp --dport 853 -j DROP",
        "-A FORWARD -i awg0 -p udp --dport 853 -j DROP",
    ):
        assert rule in rules
    assert rules.index("-A FORWARD -i awg0 -p udp --dport 53 -j DROP") < rules.index(
        "-A FORWARD -i awg0 ! -o awg0 -j ACCEPT"
    )


def test_without_the_filter_no_dns_rules_are_added(tmp_path: Path) -> None:
    agent = make_agent(tmp_path, False)
    agent._firewall("awg0")
    rules = iptables_rules(agent)
    assert not any("--dport 53" in rule or "--dport 853" in rule for rule in rules)


def test_start_resolver_writes_config_and_starts_unbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []

    class FakeProcess:
        def poll(self) -> None:
            return None

    def fake_popen(args: list[str], **_: Any) -> FakeProcess:
        started.append(list(args))
        return FakeProcess()

    monkeypatch.setattr(agent_module.subprocess, "Popen", fake_popen)
    agent = make_agent(tmp_path, True)
    agent.start_resolver()
    conf = tmp_path / "state" / "dns" / "unbound.conf"
    blocklist_file = tmp_path / "state" / "dns" / "blocklist.conf"
    assert conf.is_file()
    assert blocklist_file.is_file()
    assert "interface: 10.67.0.1" in conf.read_text(encoding="utf-8")
    assert len(started) == 1
    assert started[0][1:3] == ["-d", "-c"]
    assert started[0][3] == str(conf)
    # A second call while it runs does not start another copy.
    agent.start_resolver()
    assert len(started) == 1


def _zone_bytes(count: int) -> bytes:
    lines = [b"# zone\n"]
    lines.extend(
        f'local-zone: "n{i}.example" always_nxdomain\n'.encode() for i in range(count)
    )
    return b"".join(lines)


def _install_popen(monkeypatch: pytest.MonkeyPatch, processes: list[Any]) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.alive = True

        def poll(self) -> int | None:
            return None if self.alive else 1

    def fake_popen(args: list[str], **_: Any) -> FakeProcess:
        proc = FakeProcess()
        processes.append(proc)
        return proc

    monkeypatch.setattr(agent_module.subprocess, "Popen", fake_popen)


def test_oversized_zone_is_set_aside_and_filter_starts_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(agent_module, "MAX_ZONE_ENTRIES", 3)
    agent = make_agent(tmp_path, True)
    zone = tmp_path / "state" / "dns" / "blocklist.conf"
    original = _zone_bytes(4)
    zone.parent.mkdir(parents=True)
    zone.write_bytes(original)
    processes: list[Any] = []
    _install_popen(monkeypatch, processes)
    with caplog.at_level(logging.WARNING, logger="awg-agent"):
        agent.start_resolver()
    aside = zone.with_name("blocklist.conf.too-large")
    assert aside.read_bytes() == original
    assert "local-zone" not in zone.read_text(encoding="utf-8")
    assert zone.read_text(encoding="utf-8") == agent_module.BLOCK_ZONE_HEADER
    assert len(processes) == 1
    assert "rebuild with scripts/awg_dns_update.sh" in caplog.text
    assert "4" in caplog.text
    assert "3" in caplog.text


def test_zone_within_limit_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_module, "MAX_ZONE_ENTRIES", 3)
    agent = make_agent(tmp_path, True)
    zone = tmp_path / "state" / "dns" / "blocklist.conf"
    original = _zone_bytes(3)
    zone.parent.mkdir(parents=True)
    zone.write_bytes(original)
    processes: list[Any] = []
    _install_popen(monkeypatch, processes)
    agent.start_resolver()
    assert zone.read_bytes() == original
    assert not zone.with_name("blocklist.conf.too-large").exists()
    assert len(processes) == 1


def test_watchdog_restarts_a_dead_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_module, "RESOLVER_CHECK_SECONDS", 0.05)
    processes: list[Any] = []
    _install_popen(monkeypatch, processes)
    agent = make_agent(tmp_path, True)
    agent.start_resolver()
    assert len(processes) == 1
    processes[0].alive = False
    agent.start_resolver_watchdog()
    try:
        deadline = time.monotonic() + 2.0
        while len(processes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(processes) == 2
        assert agent._resolver_restarts == 1
        assert agent.resolver_running()
    finally:
        agent._resolver_stop.set()


def test_reload_of_a_dead_resolver_starts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[list[str]] = []
    signalled: list[int] = []

    class Dead:
        def poll(self) -> int:
            return 1

        def send_signal(self, sig: int) -> None:
            signalled.append(sig)

    class Alive:
        def poll(self) -> None:
            return None

    def fake_popen(args: list[str], **_: Any) -> Alive:
        started.append(list(args))
        return Alive()

    monkeypatch.setattr(agent_module.subprocess, "Popen", fake_popen)
    agent = make_agent(tmp_path, True)
    agent._resolver = Dead()
    agent.reload_resolver()
    assert len(started) == 1
    assert started[0][1:3] == ["-d", "-c"]
    assert signalled == []
    assert agent.resolver_running()


def test_watchdog_does_nothing_without_the_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_module, "RESOLVER_CHECK_SECONDS", 0.05)
    started: list[list[str]] = []

    class Dead:
        def poll(self) -> int:
            return 1

    def fake_popen(args: list[str], **_: Any) -> Dead:
        started.append(list(args))
        return Dead()

    monkeypatch.setattr(agent_module.subprocess, "Popen", fake_popen)
    agent = make_agent(tmp_path, False)
    agent._resolver = Dead()
    try:
        agent.check_resolver()
        agent.start_resolver_watchdog()
        time.sleep(0.2)
        assert started == []
        assert agent._resolver_restarts == 0
    finally:
        agent._resolver_stop.set()
