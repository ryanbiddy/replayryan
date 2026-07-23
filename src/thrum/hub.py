"""Read-only loopback status page for the local product suite."""

from __future__ import annotations

import html
import json
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from thrum.status import collect_status


HOST = "127.0.0.1"
DEFAULT_PORT = 5178
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_STATE_LABELS = {
    "available": "Available",
    "absent": "Absent",
    "unconfigured": "Unconfigured",
    "unhealthy": "Needs attention",
}


def _host_allowed(value: str | None, port: int) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        return False
    try:
        parsed = urllib.parse.urlsplit("//" + value)
        parsed_port = parsed.port
    except ValueError:
        return False
    return (
        parsed.hostname in _LOOPBACK_HOSTS
        and parsed.username is None
        and parsed.password is None
        and parsed_port == port
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
    )


def _safe_peer_link(value: object) -> str | None:
    if not isinstance(value, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return None
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or "\\" in parsed.path
        or parsed.query
    ):
        return None
    return value


def _link_rows(product: dict[str, Any]) -> str:
    ui = product.get("resident", {}).get("ui")
    if not isinstance(ui, dict):
        return ""
    candidates: list[tuple[str, object]] = [("Open", ui.get("home"))]
    routes = ui.get("routes")
    if isinstance(routes, dict):
        candidates.extend(
            (str(name).replace("_", " ").title(), url)
            for name, url in routes.items()
        )
    seen: set[str] = set()
    links: list[str] = []
    for label, candidate in candidates:
        url = _safe_peer_link(candidate)
        if url is None or url in seen:
            continue
        seen.add(url)
        links.append(
            '<a class="product-link" rel="noreferrer" target="_blank" '
            f'href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
        )
    if not links:
        return ""
    return '<div class="links">' + "".join(links) + "</div>"


def _product_detail(product: dict[str, Any]) -> str:
    state = product["state"]
    resident = product["resident"]
    health = resident.get("health")
    if state == "unhealthy":
        return str(product["peer"]["error"]["message"])
    if product["running"] and isinstance(health, dict):
        return f"Resident service reports {health['state'].replace('_', ' ')}."
    if product["launchable"] is True:
        return "Its stdio MCP configuration is installed and launchable."
    if state == "unconfigured":
        return "The public service is reachable; this hub deliberately reads no token."
    return "No running service or launchable command was discovered."


def _product_card(product: dict[str, Any]) -> str:
    state = str(product["state"])
    css_state = state if state in _STATE_LABELS else "unhealthy"
    health = product["resident"].get("health")
    health_state = (
        str(health["state"]).replace("_", " ")
        if isinstance(health, dict)
        else "not running"
    )
    installed = product["installed"]
    installed_label = (
        "yes" if installed is True else "no" if installed is False else "unknown"
    )
    facts = [
        ("Installed", installed_label),
        ("Resident", "running" if product["running"] else "stopped"),
        (
            "MCP",
            "launchable"
            if product["launchable"] is True
            else "not discovered"
            if product["launchable"] is False
            else "not exposed",
        ),
        ("Health", health_state),
    ]
    fact_html = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(value)}</dd>"
        for label, value in facts
    )
    return (
        f'<article class="product {css_state}">'
        '<div class="product-head">'
        f"<h2>{html.escape(str(product['name']))}</h2>"
        f'<span class="badge">{html.escape(_STATE_LABELS.get(state, "Needs attention"))}</span>'
        "</div>"
        f'<p class="detail">{html.escape(_product_detail(product))}</p>'
        f"<dl>{fact_html}</dl>"
        f"{_link_rows(product)}"
        "</article>"
    )


def _workflow(payload: dict[str, Any]) -> str:
    steps = []
    for position, stage in enumerate(payload["workflow"], 1):
        reachable = stage["reachable"] is True
        css_state = "ready" if reachable else "blocked"
        label = "Ready" if reachable else "Blocked"
        steps.append(
            f'<li class="workflow-step {css_state}">'
            f'<span class="step-number">{position}</span>'
            "<div>"
            f"<h3>{html.escape(str(stage['stage']).title())}</h3>"
            f'<p>{html.escape(str(stage["detail"]))}</p>'
            "</div>"
            f'<span class="step-state">{label}</span>'
            "</li>"
        )
    return "".join(steps)


def render_page(payload: dict[str, Any]) -> bytes:
    family_state = "Calm" if payload["ok"] else "Needs attention"
    products = "".join(_product_card(product) for product in payload["products"])
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Thrum · Local suite status</title>
  <link rel="stylesheet" href="/style.css">
</head>
<body>
  <main>
    <header class="hero">
      <div>
        <p class="eyebrow">Thrum · local suite</p>
        <h1>Grab. Study. Write.</h1>
        <p class="lede">One read-only view of Uoink, Zing, and Writer. Each
        product keeps its own data, token, process, and MCP server.</p>
      </div>
      <div class="family">
        <span>Family state</span>
        <strong>{html.escape(family_state)}</strong>
      </div>
    </header>
    <section aria-labelledby="workflow-title">
      <div class="section-head">
        <div>
          <p class="eyebrow">Workflow</p>
          <h2 id="workflow-title">What can move right now</h2>
        </div>
        <a class="refresh" href="/">Check again</a>
      </div>
      <ol class="workflow">{_workflow(payload)}</ol>
    </section>
    <section aria-labelledby="products-title">
      <div class="section-head">
        <div>
          <p class="eyebrow">Products</p>
          <h2 id="products-title">Installed is not the same as running</h2>
        </div>
        <time datetime="{html.escape(str(payload['checked_at']), quote=True)}">
          {html.escape(str(payload['checked_at']))}
        </time>
      </div>
      <div class="product-grid">{products}</div>
    </section>
    <footer>
      <p>Read-only and loopback-only. Thrum never reads product credentials,
      proxies MCP, launches peers, posts, publishes, or spends.</p>
      <p>Artifact references remain unavailable until the products ratify
      read-list contracts.</p>
    </footer>
  </main>
</body>
</html>
"""
    return document.encode("utf-8")


class StatusCache:
    def __init__(self, collector: Callable[[], dict[str, Any]]) -> None:
        self._collector = collector
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._collector()


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        collector: Callable[[], dict[str, Any]],
    ) -> None:
        self.cache = StatusCache(collector)
        super().__init__(address, HubHandler)


class HubHandler(BaseHTTPRequestHandler):
    server: HubServer

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; frame-ancestors 'none'; "
            "form-action 'none'; base-uri 'none'; img-src 'none'; "
            "connect-src 'none'",
        )
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _dispatch(self, *, head_only: bool = False) -> None:
        port = int(self.server.server_address[1])
        if not _host_allowed(self.headers.get("Host"), port):
            self._send(
                421,
                b"Loopback Host required.\n",
                "text/plain; charset=utf-8",
                head_only=head_only,
            )
            return
        try:
            target = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._send(
                400,
                b"Invalid request target.\n",
                "text/plain; charset=utf-8",
                head_only=head_only,
            )
            return
        if target.query or target.fragment:
            self._send(
                400,
                b"Query strings are not accepted.\n",
                "text/plain; charset=utf-8",
                head_only=head_only,
            )
            return
        if target.path == "/":
            try:
                body = render_page(self.server.cache.get())
            except Exception:
                body = b"Status collection failed.\n"
                self._send(
                    500,
                    body,
                    "text/plain; charset=utf-8",
                    head_only=head_only,
                )
                return
            self._send(
                200,
                body,
                "text/html; charset=utf-8",
                head_only=head_only,
            )
            return
        if target.path == "/style.css":
            body = resources.files("thrum").joinpath("web/style.css").read_bytes()
            self._send(
                200,
                body,
                "text/css; charset=utf-8",
                head_only=head_only,
            )
            return
        if target.path == "/health":
            body = json.dumps(
                {"ok": True, "service": "thrum", "transport": "loopback-http"}
            ).encode("utf-8")
            self._send(
                200,
                body,
                "application/json; charset=utf-8",
                head_only=head_only,
            )
            return
        self._send(
            404,
            b"Not found.\n",
            "text/plain; charset=utf-8",
            head_only=head_only,
        )

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch(head_only=True)

    def _method_not_allowed(self) -> None:
        port = int(self.server.server_address[1])
        if not _host_allowed(self.headers.get("Host"), port):
            self._send(
                421,
                b"Loopback Host required.\n",
                "text/plain; charset=utf-8",
            )
            return
        self._send(
            405,
            b"Read-only server.\n",
            "text/plain; charset=utf-8",
        )

    do_POST = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed

    def log_message(self, format: str, *args: object) -> None:
        return


def create_server(
    *,
    port: int = DEFAULT_PORT,
    timeout: float = 1.0,
    collector: Callable[[], dict[str, Any]] | None = None,
) -> HubServer:
    if collector is None:
        collector = lambda: collect_status(timeout=timeout, include_links=True)
    return HubServer((HOST, port), collector)


def serve(*, port: int = DEFAULT_PORT, timeout: float = 1.0) -> int:
    server = create_server(port=port, timeout=timeout)
    actual_port = int(server.server_address[1])
    print(f"Thrum: http://{HOST}:{actual_port}", flush=True)
    print("Read-only; press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
