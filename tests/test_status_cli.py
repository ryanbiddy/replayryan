from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from thrum import cli
from thrum.discovery import UOINK, WRITER
from thrum.status import collect_status


def peer(service_id, state, *, error=None):
    payload = {
        "ok": state != "unhealthy",
        "contract": "ryan.suite.peer",
        "version": 1,
        "peer": service_id,
        "state": state,
    }
    if error is None:
        payload["capabilities"] = []
    else:
        payload["error"] = error
    return payload


def resident(spec, state="absent", *, running=False):
    return {
        "supported": True,
        "running": running,
        "discovery": "default",
        "health": (
            {
                "ok": True,
                "state": "ready",
                "service_version": "1.2.3",
                "checks": [],
            }
            if running
            else None
        ),
        "peer": peer(spec.service_id, state),
    }


class StatusAndCliTests(unittest.TestCase):
    def test_status_surfaces_workflow_and_never_probes_zing_http(self):
        resident_calls = []

        def resident_probe(spec, **kwargs):
            resident_calls.append(spec.service_id)
            if spec is UOINK:
                return resident(spec, "unconfigured", running=True)
            return resident(spec)

        def mcp_probe(spec):
            launchable = spec.service_id in {"zing", "writer"}
            return {
                "supported": True,
                "transport": "stdio",
                "launchable": launchable,
                "discovery": "print_config" if launchable else "not_exposed",
                "state": "available" if launchable else "unknown",
            }

        payload = collect_status(
            resident_probe=resident_probe,
            mcp_probe=mcp_probe,
        )
        self.assertEqual(resident_calls, ["uoink", "writer"])
        self.assertTrue(payload["ok"])
        self.assertEqual(
            [stage["reachable"] for stage in payload["workflow"]],
            [True, True, True],
        )
        self.assertTrue(
            all(
                stage["reference_state"] == "not_exposed"
                for stage in payload["workflow"]
            )
        )
        self.assertEqual(payload["limits"]["zing_http"], "not_probed")

    def test_unhealthy_product_makes_family_and_cli_fail(self):
        payload = {
            "format": "thrum.status",
            "version": 1,
            "checked_at": "2026-07-23T08:00:00Z",
            "ok": False,
            "products": [
                {
                    "service_id": "uoink",
                    "name": "Uoink",
                    "state": "unhealthy",
                    "installed": True,
                    "running": False,
                    "launchable": None,
                    "configuration": "not_assessed",
                    "peer": peer(
                        "uoink",
                        "unhealthy",
                        error={
                            "code": "invalid_lease",
                            "message": "runtime lease is invalid",
                            "retryable": False,
                        },
                    ),
                    "resident": {"health": None},
                    "mcp": {},
                }
            ],
            "workflow": [],
            "limits": {},
        }
        output = io.StringIO()
        with mock.patch.object(cli, "collect_status", return_value=payload):
            with contextlib.redirect_stdout(output):
                result = cli.main(["status", "--json"])
        self.assertEqual(result, 1)
        decoded = json.loads(output.getvalue())
        self.assertFalse(decoded["ok"])

    def test_json_output_is_bounded_and_path_free(self):
        output = io.StringIO()

        def resident_probe(spec, **kwargs):
            return resident(spec)

        def mcp_probe(spec):
            return {
                "supported": True,
                "transport": "stdio",
                "launchable": False,
                "discovery": "path",
                "state": "absent",
            }

        payload = collect_status(
            resident_probe=resident_probe,
            mcp_probe=mcp_probe,
        )
        rendered = json.dumps(payload)
        self.assertNotIn("token", rendered.lower())
        self.assertNotIn("E:\\\\", rendered)
        self.assertNotIn("/home/", rendered)
        self.assertNotIn("127.0.0.1", rendered)


if __name__ == "__main__":
    unittest.main()
