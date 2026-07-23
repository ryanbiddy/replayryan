from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from thrum.discovery import (
    UOINK,
    WRITER,
    ZING,
    PeerError,
    TransportError,
    _base_url,
    _service_path,
    probe_mcp,
    probe_resident,
    validate_runtime_lease,
)


def manifest(spec, version="1.2.3"):
    return {
        "ok": True,
        "contract": "ryan.suite.service",
        "version": 1,
        "service": {
            "id": spec.service_id,
            "name": spec.name,
            "service_version": version,
            "api_version": 1,
            "resident": True,
            "default_port": spec.default_port,
            "health": {
                "contract": "ryan.suite.health",
                "version": 1,
                "href": "/api/suite/v1/health",
            },
            "capabilities": list(spec.capabilities),
            "ui": {
                "home": spec.ui_home,
                "routes": dict(spec.ui_routes),
            },
            "mcp": {"name": spec.service_id, "transport": "stdio"},
        },
    }


def health(spec, version="1.2.3", *, failed=False):
    checks = [
        {
            "id": check_id,
            "required": True,
            "status": "failed" if failed and index == 0 else "ready",
        }
        for index, check_id in enumerate(spec.health_checks)
    ]
    return {
        "ok": not failed,
        "contract": "ryan.suite.health",
        "version": 1,
        "service_id": spec.service_id,
        "service_version": version,
        "state": "needs_attention" if failed else "ready",
        "checks": checks,
    }


def lease(spec, base=None):
    base = base or spec.default_url
    return {
        "contract": "ryan.suite.runtime-lease",
        "version": 1,
        "service_id": spec.service_id,
        "service_version": "1.2.3",
        "api_version": 1,
        "base_url": base,
        "health_url": f"{base}/api/suite/v1/health",
        "manifest_url": f"{base}/.well-known/suite-service.json",
        "capabilities": list(spec.capabilities),
        "ui": {"home": spec.ui_home, "routes": dict(spec.ui_routes)},
        "pid": os.getpid(),
        "started_at": "2026-07-23T08:00:00Z",
    }


class DiscoveryTests(unittest.TestCase):
    def test_url_validator_rejects_non_loopback_and_ambiguous_forms(self):
        rejected = [
            "https://127.0.0.1:5179",
            "http://attacker.test:5179",
            "http://user@127.0.0.1:5179",
            "http://127.0.0.1",
            "http://127.0.0.1:5179/path",
            "http://127.0.0.1:5179?token=x",
            "//127.0.0.1:5179",
        ]
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(PeerError):
                _base_url(value, code="invalid_configuration")
        self.assertEqual(
            _base_url("http://[::1]:5181/", code="invalid_configuration"),
            "http://[::1]:5181",
        )

    def test_ui_paths_reject_network_backslash_and_absolute_urls(self):
        for value in (
            "//attacker/x",
            "/\\attacker",
            "https://attacker/x",
            "/dashboard\nhost",
        ):
            with self.subTest(value=value), self.assertRaises(PeerError):
                _service_path(value, code="invalid_lease")
        self.assertEqual(
            _service_path("/dashboard#library", code="invalid_lease"),
            "/dashboard#library",
        )

    def test_runtime_lease_exact_shape_and_live_pid(self):
        self.assertEqual(
            validate_runtime_lease(lease(UOINK), UOINK)["service_id"],
            "uoink",
        )
        bad = lease(UOINK)
        bad["token"] = "secret"
        with self.assertRaisesRegex(PeerError, "does not match"):
            validate_runtime_lease(bad, UOINK)
        with self.assertRaises(PeerError) as caught:
            validate_runtime_lease(lease(UOINK), UOINK, pid_checker=lambda _: False)
        self.assertEqual(caught.exception.code, "stale_lease")

    def test_invalid_lease_stops_before_network(self):
        with tempfile.TemporaryDirectory() as raw:
            registry = Path(raw)
            payload = lease(UOINK)
            payload["base_url"] = "http://attacker.test:5179"
            path = registry / "uoink.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            called = False

            def fetcher(url, timeout):
                nonlocal called
                called = True
                raise AssertionError("network must not be called")

            result = probe_resident(
                UOINK,
                registry_dir=registry,
                json_fetcher=fetcher,
            )
        self.assertFalse(called)
        self.assertEqual(result["peer"]["state"], "unhealthy")
        self.assertEqual(result["peer"]["error"]["code"], "invalid_lease")

    def test_non_regular_lease_is_invalid_not_absent(self):
        with tempfile.TemporaryDirectory() as raw:
            registry = Path(raw)
            (registry / "uoink.json").mkdir()
            result = probe_resident(
                UOINK,
                registry_dir=registry,
                json_fetcher=lambda url, timeout: self.fail("must not fetch"),
            )
        self.assertEqual(result["peer"]["state"], "unhealthy")
        self.assertEqual(result["peer"]["error"]["code"], "invalid_lease")

    def test_default_refusal_is_absent_but_explicit_refusal_is_unhealthy(self):
        def refused(url, timeout):
            raise TransportError(
                "unavailable",
                "refused",
                retryable=True,
                absence_eligible=True,
            )

        with tempfile.TemporaryDirectory() as raw:
            default = probe_resident(
                UOINK,
                registry_dir=Path(raw),
                json_fetcher=refused,
            )
            explicit = probe_resident(
                UOINK,
                explicit_url="http://127.0.0.1:9999",
                registry_dir=Path(raw),
                json_fetcher=refused,
            )
        self.assertEqual(default["peer"]["state"], "absent")
        self.assertEqual(explicit["peer"]["state"], "unhealthy")
        self.assertEqual(explicit["peer"]["error"]["code"], "unavailable")

    def test_live_public_contract_is_unconfigured_without_token(self):
        payloads = [manifest(UOINK), health(UOINK)]

        def fetcher(url, timeout):
            return payloads.pop(0)

        with tempfile.TemporaryDirectory() as raw:
            result = probe_resident(
                UOINK,
                registry_dir=Path(raw),
                json_fetcher=fetcher,
            )
        self.assertTrue(result["running"])
        self.assertEqual(result["health"]["state"], "ready")
        self.assertEqual(result["peer"]["state"], "unconfigured")

    def test_wrong_identity_and_failed_health_are_distinct(self):
        wrong = manifest(UOINK)
        wrong["service"]["id"] = "writer"
        with tempfile.TemporaryDirectory() as raw:
            wrong_result = probe_resident(
                UOINK,
                registry_dir=Path(raw),
                json_fetcher=lambda url, timeout: wrong,
            )
            payloads = [manifest(UOINK), health(UOINK, failed=True)]
            failed_result = probe_resident(
                UOINK,
                registry_dir=Path(raw),
                json_fetcher=lambda url, timeout: payloads.pop(0),
            )
        self.assertEqual(wrong_result["peer"]["error"]["code"], "wrong_service")
        self.assertEqual(failed_result["peer"]["error"]["code"], "peer_unhealthy")

    def test_zing_print_config_accepts_comments_without_http_probe(self):
        output = """# Claude Desktop
{
  "mcpServers": {
    "zing": {
      "command": "C:\\\\Python\\\\python.exe",
      "args": ["-m", "myzing.cli", "serve-mcp"]
    }
  }
}
# another command
"""

        def runner(args, **kwargs):
            self.assertEqual(
                args[1:],
                ["serve-mcp", "--print-config", "desktop"],
            )
            return subprocess.CompletedProcess(args, 0, output, "")

        result = probe_mcp(
            ZING,
            which=lambda name: "zing-command",
            runner=runner,
        )
        self.assertTrue(result["launchable"])
        self.assertEqual(result["state"], "available")

    def test_writer_invalid_print_config_is_unhealthy(self):
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                0,
                '{"mcpServers":{"writer":{"command":"python","args":[]}}}',
                "",
            )

        result = probe_mcp(
            WRITER,
            which=lambda name: "writer-command",
            runner=runner,
        )
        self.assertEqual(result["state"], "unhealthy")
        self.assertEqual(result["error"]["code"], "invalid_configuration")


if __name__ == "__main__":
    unittest.main()
