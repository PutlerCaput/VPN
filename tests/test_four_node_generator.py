"""Offline tests: no SSH, external Xray, Docker daemon or server access."""

# ruff: noqa: PT009, PT027  -- stdlib unittest is intentional here.

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.four_node import generate

INV = {
    "ru1": "1.1.1.1",
    "ru2": "8.8.8.8",
    "nl1": "9.9.9.9",
    "nl2": "4.2.2.2",
    "server_name": "www.example.org",
    "target": "www.example.org:443",
}
NL1 = generate.Credentials(
    private="A" * 43,
    public="B" * 43,
    short_id="a" * 16,
    user_id="00000000-0000-4000-8000-000000000001",
)
NL2 = generate.Credentials(
    private="C" * 43,
    public="D" * 43,
    short_id="b" * 16,
    user_id="00000000-0000-4000-8000-000000000002",
)


class FourNodeTests(unittest.TestCase):
    def test_bundle_matches_paired_keys_and_backends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "private-bundle"
            generate.render(output, INV, NL1, NL2)
            nl1 = json.loads((output / "nl1/config.json").read_text())
            nl2 = json.loads((output / "nl2/config.json").read_text())
            self.assertEqual(
                nl1["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"],
                NL1.private,
            )
            self.assertEqual(
                nl2["inbounds"][0]["streamSettings"]["realitySettings"]["privateKey"],
                NL2.private,
            )
            self.assertNotEqual(NL1.private, NL2.private)
            self.assertIn("9.9.9.9:443", (output / "ru1/haproxy.cfg").read_text())
            self.assertNotIn("4.2.2.2:443", (output / "ru1/haproxy.cfg").read_text())
            self.assertIn("4.2.2.2:443", (output / "ru2/haproxy.cfg").read_text())
            self.assertNotIn("9.9.9.9:443", (output / "ru2/haproxy.cfg").read_text())
            links = (output / "vless-links.txt").read_text().splitlines()
            self.assertEqual(len(links), 4)
            self.assertIn("pbk=" + NL1.public, links[0])
            self.assertIn("pbk=" + NL2.public, links[1])
            self.assertIn("@" + INV["ru1"] + ":443", links[0])
            self.assertIn("@" + INV["ru2"] + ":443", links[1])
            self.assertIn("@" + INV["nl1"] + ":443", links[2])
            self.assertIn("@" + INV["nl2"] + ":443", links[3])
            self.assertEqual((output / "nl1/config.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)

    def test_refuses_existing_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "private-bundle"
            generate.render(output, INV, NL1, NL2)
            with self.assertRaises(FileExistsError):
                generate.render(output, INV, NL1, NL2)

    def test_refuses_bundle_inside_repo(self) -> None:
        with self.assertRaises(ValueError):
            generate.render(generate.REPO_ROOT / "private-generated", INV, NL1, NL2)

    def test_inventory_checks_ips_duplicates_and_sni_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            for change in (
                {"ru1": "192.0.2.1"},
                {"nl2": INV["nl1"]},
                {"target": "another.example:443"},
                {"server_name": "invalid_domain"},
            ):
                path.write_text(json.dumps({**INV, **change}))
                with self.subTest(change=change), self.assertRaises(ValueError):
                    generate.load_inventory(path)
            path.write_text(json.dumps(INV))
            self.assertEqual(generate.load_inventory(path), INV)

    def test_xray_output_parser_accepts_password_label(self) -> None:
        from subprocess import CompletedProcess

        output = f"PrivateKey: {'A' * 43}\nPassword: {'B' * 43}\nHash32: test\n"
        with patch(
            "deploy.four_node.generate.subprocess.run",
            return_value=CompletedProcess(["xray", "x25519"], 0, output),
        ) as mocked:
            self.assertEqual(generate.x25519("xray"), ("A" * 43, "B" * 43))
            mocked.assert_called_once()
            self.assertFalse(mocked.call_args.kwargs.get("shell", False))

    def test_xray_26_password_publickey_label(self) -> None:
        from subprocess import CompletedProcess

        output = f"PrivateKey: {'A' * 43}\nPassword (PublicKey): {'B' * 43}\nHash32: test\n"
        with patch(
            "deploy.four_node.generate.subprocess.run",
            return_value=CompletedProcess(["xray", "x25519"], 0, output),
        ):
            self.assertEqual(generate.x25519("xray"), ("A" * 43, "B" * 43))

    def test_no_implicit_cross_node_failover(self) -> None:
        self.assertNotIn("server nl2", generate.haproxy_config(INV["nl1"]))
        self.assertNotIn("send-proxy", generate.haproxy_config(INV["nl1"]))
        self.assertNotIn("http-request", generate.haproxy_config(INV["nl1"]))


if __name__ == "__main__":
    unittest.main()
