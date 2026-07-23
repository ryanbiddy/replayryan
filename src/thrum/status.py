"""Aggregate resident health and MCP launchability without controlling peers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from thrum.discovery import (
    UOINK,
    WRITER,
    ZING,
    ServiceSpec,
    probe_mcp,
    probe_resident,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _available_peer(service_id: str) -> dict[str, Any]:
    return {
        "ok": True,
        "contract": "ryan.suite.peer",
        "version": 1,
        "peer": service_id,
        "state": "available",
        "capabilities": [],
    }


def _absent_resident(service_id: str) -> dict[str, Any]:
    return {
        "supported": False,
        "running": False,
        "discovery": "not_applicable",
        "health": None,
        "peer": {
            "ok": True,
            "contract": "ryan.suite.peer",
            "version": 1,
            "peer": service_id,
            "state": "absent",
            "capabilities": [],
        },
    }


def _product(
    spec: ServiceSpec,
    resident: dict[str, Any],
    mcp: dict[str, Any],
) -> dict[str, Any]:
    resident_peer = resident["peer"]
    if resident_peer["state"] == "unhealthy":
        peer = resident_peer
    elif resident_peer["state"] in {"available", "unconfigured"}:
        peer = resident_peer
    elif mcp.get("state") == "unhealthy":
        error = mcp["error"]
        peer = {
            "ok": False,
            "contract": "ryan.suite.peer",
            "version": 1,
            "peer": spec.service_id,
            "state": "unhealthy",
            "error": dict(error),
        }
    elif mcp.get("launchable") is True:
        peer = _available_peer(spec.service_id)
    else:
        peer = resident_peer
    lease_evidence = resident.get("discovery") == "lease"
    if resident.get("running") or lease_evidence or mcp.get("launchable") is True:
        installed: bool | None = True
    elif (
        resident_peer["state"] == "absent"
        and mcp.get("state") in {"absent", "unknown"}
    ):
        installed = False
    else:
        installed = None
    return {
        "service_id": spec.service_id,
        "name": spec.name,
        "state": peer["state"],
        "installed": installed,
        "running": bool(resident.get("running")),
        "launchable": mcp.get("launchable"),
        "configuration": "not_assessed",
        "peer": peer,
        "resident": resident,
        "mcp": mcp,
    }


def _workflow(products: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    uoink = products["uoink"]
    zing = products["zing"]
    writer = products["writer"]
    stages = [
        (
            "grab",
            bool(
                uoink["running"]
                and uoink["resident"].get("health")
                and uoink["resident"]["health"]["ok"]
            ),
            "Uoink public health is ready"
            if uoink["running"]
            else "Uoink is not running",
        ),
        (
            "study",
            zing["mcp"].get("launchable") is True,
            "Zing MCP configuration is launchable"
            if zing["mcp"].get("launchable") is True
            else "Zing MCP configuration is not launchable",
        ),
        (
            "write",
            bool(writer["running"] or writer["mcp"].get("launchable") is True),
            "Writer is running or its MCP configuration is launchable"
            if writer["running"] or writer["mcp"].get("launchable") is True
            else "Writer is not running or launchable",
        ),
    ]
    return [
        {
            "stage": stage,
            "reachable": reachable,
            "detail": detail,
            "reference": None,
            "reference_state": "not_exposed",
        }
        for stage, reachable, detail in stages
    ]


def collect_status(
    *,
    uoink_url: str | None = None,
    writer_url: str | None = None,
    registry_dir: Path | None = None,
    timeout: float = 1.0,
    resident_probe: Callable[..., dict[str, Any]] = probe_resident,
    mcp_probe: Callable[..., dict[str, Any]] = probe_mcp,
) -> dict[str, Any]:
    uoink_resident = resident_probe(
        UOINK,
        explicit_url=uoink_url,
        registry_dir=registry_dir,
        timeout=timeout,
    )
    writer_resident = resident_probe(
        WRITER,
        explicit_url=writer_url,
        registry_dir=registry_dir,
        timeout=timeout,
    )
    products = {
        "uoink": _product(
            UOINK,
            uoink_resident,
            mcp_probe(UOINK),
        ),
        "writer": _product(
            WRITER,
            writer_resident,
            mcp_probe(WRITER),
        ),
        "zing": _product(
            ZING,
            _absent_resident("zing"),
            mcp_probe(ZING),
        ),
    }
    ordered = [products[name] for name in ("uoink", "zing", "writer")]
    return {
        "format": "thrum.status",
        "version": 1,
        "checked_at": _now(),
        "ok": all(product["state"] != "unhealthy" for product in ordered),
        "products": ordered,
        "workflow": _workflow(products),
        "limits": {
            "credentials": "not_read",
            "artifact_references": "not_exposed",
            "zing_http": "not_probed",
            "trusted_install_catalog": "not_available",
        },
    }
