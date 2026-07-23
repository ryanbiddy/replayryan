"""Build, install, and exercise Thrum from a fresh temporary environment."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import venv
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=capture_output,
        text=True,
        encoding="utf-8",
    )


def _handler(
    *,
    service_id: str,
    name: str,
    default_port: int,
    capabilities: list[str],
    ui: dict[str, object],
    checks: list[str],
) -> type[BaseHTTPRequestHandler]:
    manifest = {
        "ok": True,
        "contract": "ryan.suite.service",
        "version": 1,
        "service": {
            "id": service_id,
            "name": name,
            "service_version": "9.9.9-test",
            "api_version": 1,
            "resident": True,
            "default_port": default_port,
            "health": {
                "contract": "ryan.suite.health",
                "version": 1,
                "href": "/api/suite/v1/health",
            },
            "capabilities": capabilities,
            "ui": ui,
            "mcp": {"name": service_id, "transport": "stdio"},
        },
    }
    health = {
        "ok": True,
        "contract": "ryan.suite.health",
        "version": 1,
        "service_id": service_id,
        "service_version": "9.9.9-test",
        "state": "ready",
        "checks": [
            {"id": check_id, "required": True, "status": "ready"}
            for check_id in checks
        ],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/.well-known/suite-service.json":
                payload = manifest
                status = 200
            elif self.path == "/api/suite/v1/health":
                payload = health
                status = 200
            else:
                payload = {"ok": False}
                status = 404
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


@contextmanager
def _service(**kwargs: object) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(**kwargs))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="thrum-clean-") as raw:
        base = Path(raw)
        environment = base / "venv"
        wheelhouse = base / "wheelhouse"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = (
            environment / "Scripts" / "python.exe"
            if os.name == "nt"
            else environment / "bin" / "python"
        )
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(wheelhouse),
                str(ROOT),
            ]
        )
        wheel = next(wheelhouse.glob("ryan_thrum-*.whl"))
        run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)])
        clean_env = os.environ.copy()
        clean_env["PATH"] = str(scripts)
        clean_env["PYTHONIOENCODING"] = "utf-8"
        run([str(python), "-m", "thrum", "--version"], env=clean_env)
        suite = scripts / ("suite.exe" if os.name == "nt" else "suite")
        thrum = scripts / ("thrum.exe" if os.name == "nt" else "thrum")
        run([str(suite), "--help"], env=clean_env)
        run([str(thrum), "--help"], env=clean_env)
        with (
            _service(
                service_id="uoink",
                name="Uoink",
                default_port=5179,
                capabilities=[
                    "uoink.corpus.read/1",
                    "uoink.engagement.ingest/1",
                    "uoink.media.handoff/1",
                ],
                ui={
                    "home": "/dashboard",
                    "routes": {"library": "/dashboard#library"},
                },
                checks=["core", "index", "corpus_paths"],
            ) as uoink_url,
            _service(
                service_id="writer",
                name="Writer",
                default_port=5181,
                capabilities=["writer.api/1", "writer.shot-list/1"],
                ui={"home": "/", "routes": {"editor": "/"}},
                checks=["core", "database"],
            ) as writer_url,
        ):
            status = run(
                [
                    str(suite),
                    "status",
                    "--json",
                    "--uoink-url",
                    uoink_url,
                    "--writer-url",
                    writer_url,
                ],
                env=clean_env,
                capture_output=True,
            )
            payload = json.loads(status.stdout)
            require(payload["ok"] is True, "installed status did not stay calm")
            require(
                [item["service_id"] for item in payload["products"]]
                == ["uoink", "zing", "writer"],
                "installed status omitted or reordered a product",
            )
            require(
                [item["reachable"] for item in payload["workflow"]]
                == [True, False, True],
                "installed status did not surface the expected workflow",
            )
            doctor = run(
                [
                    str(thrum),
                    "doctor",
                    "--uoink-url",
                    uoink_url,
                    "--writer-url",
                    writer_url,
                ],
                env=clean_env,
                capture_output=True,
            )
            require("Suite doctor" in doctor.stdout, "doctor heading is missing")
            require("Family OK" in doctor.stdout, "doctor family result is missing")
    print("clean-install: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
