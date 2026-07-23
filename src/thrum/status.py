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
    uoink_health = uoink["resident"].get("health")
    uoink_ready = bool(
        uoink["running"] and uoink_health and uoink_health["ok"]
    )
    writer_health = writer["resident"].get("health")
    writer_resident_ready = bool(
        writer["running"] and writer_health and writer_health["ok"]
    )
    writer_mcp_ready = bool(
        writer["state"] != "unhealthy"
        and writer["mcp"].get("launchable") is True
    )
    if uoink_ready:
        uoink_detail = "Uoink public health is ready"
    elif uoink["running"]:
        uoink_detail = "Uoink is running but public health needs attention"
    else:
        uoink_detail = "Uoink is not running"
    if writer_resident_ready:
        writer_detail = "Writer public health is ready"
    elif writer_mcp_ready:
        writer_detail = "Writer MCP configuration is launchable"
    elif writer["running"]:
        writer_detail = "Writer is running but public health needs attention"
    else:
        writer_detail = "Writer is not running or launchable"
    stages = [
        (
            "grab",
            uoink_ready,
            uoink_detail,
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
            writer_resident_ready or writer_mcp_ready,
            writer_detail,
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
    include_links: bool = False,
    resident_probe: Callable[..., dict[str, Any]] = probe_resident,
    mcp_probe: Callable[..., dict[str, Any]] = probe_mcp,
) -> dict[str, Any]:
    uoink_resident = resident_probe(
        UOINK,
        explicit_url=uoink_url,
        registry_dir=registry_dir,
        timeout=timeout,
        include_ui=include_links,
    )
    writer_resident = resident_probe(
        WRITER,
        explicit_url=writer_url,
        registry_dir=registry_dir,
        timeout=timeout,
        include_ui=include_links,
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
