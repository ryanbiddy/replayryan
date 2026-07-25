"""Command-line entry point for the read-only suite doctor."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from replayryan import __version__
from replayryan.status import collect_status


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="suite",
        description="Read-only local status for Uoink, Zing, and Writer.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "status"):
        command = subcommands.add_parser(
            name,
            help="check product health and workflow readiness",
        )
        command.add_argument(
            "--json",
            action="store_true",
            help="write the bounded machine-readable status",
        )
        command.add_argument(
            "--uoink-url",
            help="explicit Uoink loopback base URL",
        )
        command.add_argument(
            "--writer-url",
            help="explicit Writer loopback base URL",
        )
        command.add_argument(
            "--timeout",
            type=float,
            default=1.0,
            help="per-request timeout in seconds (default: 1.0)",
        )
    serve = subcommands.add_parser(
        "serve",
        help="serve the read-only local status page",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=5178,
        help="loopback port (default: 5178; use 0 for an ephemeral port)",
    )
    serve.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="per-product request timeout in seconds (default: 1.0)",
    )
    return parser


def _human(payload: dict[str, Any]) -> str:
    lines = ["Suite doctor"]
    for product in payload["products"]:
        state = str(product["state"]).upper()
        facts: list[str] = []
        if product["running"]:
            health = product["resident"].get("health")
            facts.append(
                "running"
                + (f"; health {health['state']}" if health is not None else "")
            )
        elif product["launchable"] is True:
            facts.append("MCP launchable")
        elif product["launchable"] is False:
            facts.append("not running; MCP command not found or invalid")
        else:
            facts.append("not running")
        if state == "UNCONFIGURED":
            facts.append("credentials deliberately not read")
        if state == "UNHEALTHY":
            error = product["peer"]["error"]
            facts.append(f"{error['code']}: {error['message']}")
        lines.append(f"  {product['name']:<7} {state:<12} {'; '.join(facts)}")
    lines.append("")
    lines.append("Workflow")
    for stage in payload["workflow"]:
        label = "READY" if stage["reachable"] else "BLOCKED"
        lines.append(f"  {stage['stage']:<6} {label:<7} {stage['detail']}")
    calm = (
        "OK — optional absence and unconfigured credentials are calm states."
        if payload["ok"]
        else "NEEDS ATTENTION — at least one discovered product is unhealthy."
    )
    lines.extend(["", f"Family {calm}"])
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 0 < args.timeout <= 30:
        _parser().error("--timeout must be greater than 0 and at most 30 seconds")
    if args.command == "serve":
        if not 0 <= args.port <= 65535:
            _parser().error("--port must be between 0 and 65535")
        from replayryan.hub import serve

        return serve(port=args.port, timeout=args.timeout)
    payload = collect_status(
        uoink_url=args.uoink_url,
        writer_url=args.writer_url,
        timeout=args.timeout,
    )
    if args.json:
        json.dump(payload, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(_human(payload))
    return 0 if payload["ok"] else 1
