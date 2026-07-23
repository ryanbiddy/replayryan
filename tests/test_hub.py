from __future__ import annotations

import contextlib
import http.client
import threading
import unittest

from thrum.hub import create_server, render_page


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


def product(
    service_id,
    name,
    state,
    *,
    running=False,
    launchable=None,
    ui=None,
    error=None,
):
    resident = {
        "supported": service_id != "zing",
        "running": running,
        "discovery": "explicit" if running else "default",
        "health": (
            {
                "ok": True,
                "state": "ready",
                "service_version": "9.9.9-test",
                "checks": [],
            }
            if running
            else None
        ),
        "peer": peer(service_id, state, error=error),
    }
    if ui is not None:
        resident["ui"] = ui
    return {
        "service_id": service_id,
        "name": name,
        "state": state,
        "installed": running or launchable is True,
        "running": running,
        "launchable": launchable,
        "configuration": "not_assessed",
        "peer": peer(service_id, state, error=error),
        "resident": resident,
        "mcp": {},
    }


def status_payload():
    return {
        "format": "thrum.status",
        "version": 1,
        "checked_at": "2026-07-23T14:00:00Z",
        "ok": True,
        "products": [
            product(
                "uoink",
                "Uoink",
                "unconfigured",
                running=True,
                ui={
                    "home": "http://127.0.0.1:5179/dashboard",
                    "routes": {
                        "library": "http://127.0.0.1:5179/dashboard#library"
                    },
                },
            ),
            product("zing", "Zing", "available", launchable=True),
            product("writer", "Writer", "absent", launchable=False),
        ],
        "workflow": [
            {
                "stage": "grab",
                "reachable": True,
                "detail": "Uoink public health is ready",
                "reference": None,
                "reference_state": "not_exposed",
            },
            {
                "stage": "study",
                "reachable": True,
                "detail": "Zing MCP configuration is launchable",
                "reference": None,
                "reference_state": "not_exposed",
            },
            {
                "stage": "write",
                "reachable": False,
                "detail": "Writer is not running or launchable",
                "reference": None,
                "reference_state": "not_exposed",
            },
        ],
        "limits": {
            "credentials": "not_read",
            "artifact_references": "not_exposed",
            "zing_http": "not_probed",
            "trusted_install_catalog": "not_available",
        },
    }


@contextlib.contextmanager
def running_server(payload=None):
    server = create_server(port=0, collector=lambda: payload or status_payload())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request(server, method, path, host):
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        server.server_address[1],
        timeout=3,
    )
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host)
    connection.endheaders()
    response = connection.getresponse()
    body = response.read()
    headers = dict(response.getheaders())
    connection.close()
    return response.status, headers, body


class HubTests(unittest.TestCase):
    def test_page_is_read_only_token_free_and_links_to_validated_peer_ui(self):
        with running_server() as server:
            port = server.server_address[1]
            status, headers, body = request(
                server,
                "GET",
                "/",
                f"127.0.0.1:{port}",
            )

        rendered = body.decode("utf-8")
        self.assertEqual(status, 200)
        self.assertIn("Grab. Study. Write.", rendered)
        self.assertIn("http://127.0.0.1:5179/dashboard#library", rendered)
        self.assertNotIn("token=", rendered.lower())
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_host_allowlist_rejects_rebinding_and_wrong_ports(self):
        with running_server() as server:
            port = server.server_address[1]
            accepted, _, _ = request(server, "GET", "/health", f"localhost:{port}")
            attacker, _, _ = request(
                server,
                "GET",
                "/health",
                f"attacker.test:{port}",
            )
            wrong_port, _, _ = request(
                server,
                "GET",
                "/health",
                "127.0.0.1:5178",
            )

        self.assertEqual(accepted, 200)
        self.assertEqual(attacker, 421)
        self.assertEqual(wrong_port, 421)

    def test_queries_and_writes_are_rejected_without_reflection(self):
        with running_server() as server:
            port = server.server_address[1]
            query_status, _, query_body = request(
                server,
                "GET",
                "/?token=secret-value",
                f"127.0.0.1:{port}",
            )
            post_status, _, post_body = request(
                server,
                "POST",
                "/",
                f"127.0.0.1:{port}",
            )

        self.assertEqual(query_status, 400)
        self.assertNotIn(b"secret-value", query_body)
        self.assertEqual(post_status, 405)
        self.assertEqual(post_body, b"Read-only server.\n")

    def test_rendering_escapes_peer_error_text(self):
        payload = status_payload()
        payload["ok"] = False
        error = {
            "code": "contract_mismatch",
            "message": '<script>alert("x")</script>',
            "retryable": False,
        }
        payload["products"][2] = product(
            "writer",
            "Writer",
            "unhealthy",
            error=error,
        )

        rendered = render_page(payload).decode("utf-8")

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_rendering_drops_untrusted_link_schemes_and_query_strings(self):
        payload = status_payload()
        payload["products"][0]["resident"]["ui"] = {
            "home": "javascript:alert(1)",
            "routes": {
                "token": "http://127.0.0.1:5179/dashboard?token=secret"
            },
        }

        rendered = render_page(payload).decode("utf-8")

        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("token=secret", rendered)
        self.assertNotIn('class="links"', rendered)

    def test_server_binds_only_ipv4_loopback(self):
        server = create_server(port=0, collector=status_payload)
        try:
            self.assertEqual(server.server_address[0], "127.0.0.1")
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
