"""Strict, token-free readers for the ratified local suite contracts."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
_LEASE_LIMIT = 64 * 1024
_JSON_LIMIT = 256 * 1024
_UTC_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
)
_LEASE_KEYS = {
    "contract",
    "version",
    "service_id",
    "service_version",
    "api_version",
    "base_url",
    "health_url",
    "manifest_url",
    "capabilities",
    "ui",
    "pid",
    "started_at",
}
_MANIFEST_KEYS = {"ok", "contract", "version", "service"}
_SERVICE_KEYS = {
    "id",
    "name",
    "service_version",
    "api_version",
    "resident",
    "default_port",
    "health",
    "capabilities",
    "ui",
    "mcp",
}
_HEALTH_KEYS = {
    "ok",
    "contract",
    "version",
    "service_id",
    "service_version",
    "state",
    "checks",
}
_CHECK_KEYS = {"id", "required", "status"}
_HEALTH_STATES = {"ready", "ready_with_limits", "needs_attention"}
_CHECK_STATUSES = {"ready", "busy", "degraded", "failed"}


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    name: str
    default_port: int
    capabilities: tuple[str, ...]
    ui_home: str
    ui_routes: tuple[tuple[str, str], ...]
    health_checks: tuple[str, ...]
    cli_name: str | None = None
    print_config_args: tuple[str, ...] = ()
    mcp_module: str | None = None

    @property
    def default_url(self) -> str:
        return f"http://127.0.0.1:{self.default_port}"


UOINK = ServiceSpec(
    service_id="uoink",
    name="Uoink",
    default_port=5179,
    capabilities=(
        "uoink.corpus.read/1",
        "uoink.engagement.ingest/1",
        "uoink.media.handoff/1",
    ),
    ui_home="/dashboard",
    ui_routes=(("library", "/dashboard#library"),),
    health_checks=("core", "index", "corpus_paths"),
)
WRITER = ServiceSpec(
    service_id="writer",
    name="Writer",
    default_port=5181,
    capabilities=("writer.api/1", "writer.shot-list/1"),
    ui_home="/",
    ui_routes=(("editor", "/"),),
    health_checks=("core", "database"),
    cli_name="writer",
    print_config_args=("serve-mcp", "--print-config"),
    mcp_module="writer.cli",
)
ZING = ServiceSpec(
    service_id="zing",
    name="Zing",
    default_port=5180,
    capabilities=(),
    ui_home="/",
    ui_routes=(),
    health_checks=(),
    cli_name="zing",
    print_config_args=("serve-mcp", "--print-config", "desktop"),
    mcp_module="myzing.cli",
)


class PeerError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class TransportError(PeerError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        absence_eligible: bool = False,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.absence_eligible = absence_eligible


@dataclass(frozen=True)
class Target:
    base_url: str
    source: str
    configured: bool
    lease_service_version: str | None = None


def _error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> PeerError:
    return PeerError(code, message, retryable=retryable)


def _exact(value: Any, keys: set[str], label: str, *, code: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise _error(code, f"{label} does not match version 1")
    return value


def _base_url(value: Any, *, code: str) -> str:
    if not isinstance(value, str):
        raise _error(code, "URL must be an HTTP loopback address")
    raw = value.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise _error(code, "URL must be an HTTP loopback address") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise _error(code, "URL must be an HTTP loopback address")
    return raw.rstrip("/")


def _service_path(value: Any, *, code: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
    ):
        raise _error(code, "service UI path must begin with exactly one slash")
    if "\\" in value or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise _error(
            code,
            "service UI path must not contain a backslash or control character",
        )
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise _error(code, "service UI path is invalid") from exc
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise _error(code, "service UI path must stay on the service origin")
    return value


def _ui(value: Any, spec: ServiceSpec, *, code: str) -> dict[str, Any]:
    value = _exact(value, {"home", "routes"}, "ui", code=code)
    home = _service_path(value["home"], code=code)
    routes = value["routes"]
    if not isinstance(routes, dict) or any(not isinstance(key, str) for key in routes):
        raise _error(code, "ui.routes must be an object of service paths")
    checked = {key: _service_path(path, code=code) for key, path in routes.items()}
    if home != spec.ui_home or checked != dict(spec.ui_routes):
        raise _error(code, f"{spec.name} UI paths do not match version 1")
    return value


def _capabilities(value: Any, spec: ServiceSpec, *, code: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or value != sorted(set(value))
        or tuple(value) != spec.capabilities
    ):
        raise _error(code, f"{spec.name} capabilities do not match version 1")
    return value


def runtime_registry_dir(
    *,
    platform_name: str | None = None,
    environ: dict[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    platform_name = sys.platform if platform_name is None else platform_name
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if platform_name == "win32":
        local = environ.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "RyanSuite" / "services.d"
    if platform_name == "darwin":
        return home / "Library" / "Application Support" / "RyanSuite" / "services.d"
    state_home = environ.get("XDG_STATE_HOME")
    base = Path(state_home) if state_home else home / ".local" / "state"
    return base / "ryan-suite" / "services.d"


def process_is_live(pid: int) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        return False
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle, ctypes.byref(exit_code)
            ):
                return False
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lease_file(path: Path) -> Any:
    try:
        details = path.lstat()
    except OSError as exc:
        raise _error("invalid_lease", "runtime lease cannot be inspected") from exc
    if not stat.S_ISREG(details.st_mode) or path.is_symlink():
        raise _error("invalid_lease", "runtime lease must be a regular file")
    if details.st_size > _LEASE_LIMIT:
        raise _error("invalid_lease", "runtime lease exceeds the size limit")
    if sys.platform != "win32":
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            raise _error("invalid_lease", "runtime lease has the wrong owner")
        if stat.S_IMODE(details.st_mode) & 0o077:
            raise _error("invalid_lease", "runtime lease permissions are not per-user")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _error("invalid_lease", "runtime lease cannot be read safely") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (details.st_dev, details.st_ino):
            raise _error("invalid_lease", "runtime lease changed while being read")
        chunks: list[bytes] = []
        remaining = _LEASE_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > _LEASE_LIMIT:
        raise _error("invalid_lease", "runtime lease exceeds the size limit")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error("invalid_lease", "runtime lease is not valid UTF-8 JSON") from exc


def validate_runtime_lease(
    payload: Any,
    spec: ServiceSpec,
    *,
    pid_checker: Callable[[int], bool] = process_is_live,
) -> dict[str, Any]:
    code = "invalid_lease"
    payload = _exact(payload, _LEASE_KEYS, "runtime lease", code=code)
    if (
        payload["contract"] != "ryan.suite.runtime-lease"
        or payload["version"] != 1
        or payload["service_id"] != spec.service_id
        or payload["api_version"] != 1
        or not isinstance(payload["service_version"], str)
        or not payload["service_version"]
    ):
        raise _error(code, f"runtime lease identity does not match {spec.name} v1")
    base = _base_url(payload["base_url"], code=code)
    if payload["health_url"] != f"{base}/api/suite/v1/health":
        raise _error(code, "runtime lease health URL is invalid")
    if payload["manifest_url"] != f"{base}/.well-known/suite-service.json":
        raise _error(code, "runtime lease manifest URL is invalid")
    _capabilities(payload["capabilities"], spec, code=code)
    _ui(payload["ui"], spec, code=code)
    pid = payload["pid"]
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        raise _error(code, "runtime lease PID is invalid")
    started_at = payload["started_at"]
    if not isinstance(started_at, str) or not _UTC_TIMESTAMP.fullmatch(started_at):
        raise _error(code, "runtime lease timestamp is invalid")
    try:
        datetime.fromisoformat(started_at[:-1] + "+00:00")
    except ValueError as exc:
        raise _error(code, "runtime lease timestamp is invalid") from exc
    if not pid_checker(pid):
        raise _error(
            "stale_lease",
            f"{spec.name} runtime lease process is no longer running",
            retryable=True,
        )
    return payload


def resolve_target(
    spec: ServiceSpec,
    *,
    explicit_url: str | None = None,
    registry_dir: Path | None = None,
    pid_checker: Callable[[int], bool] = process_is_live,
) -> Target:
    if explicit_url is not None:
        return Target(
            _base_url(explicit_url, code="invalid_configuration"),
            "explicit",
            True,
        )
    registry = runtime_registry_dir() if registry_dir is None else Path(registry_dir)
    lease_path = registry / f"{spec.service_id}.json"
    try:
        lease_path.lstat()
        present = True
    except FileNotFoundError:
        present = False
    except OSError as exc:
        raise _error("invalid_lease", "runtime lease cannot be inspected") from exc
    if present:
        try:
            lease = validate_runtime_lease(
                _read_lease_file(lease_path),
                spec,
                pid_checker=pid_checker,
            )
        except PeerError as exc:
            # The contract's discovery order is explicit URL -> *valid* lease ->
            # default. A lease whose process is gone is not a valid lease, so
            # discovery continues to the default address rather than stopping.
            #
            # This matters because a lease outlives an unclean shutdown: kill
            # `writer serve` once and the file sits there with a dead PID
            # forever. Treating that as unhealthy makes a product the user
            # simply is not running turn the whole family red, which is exactly
            # what the contract's "absence of an optional peer is calm" rule
            # exists to prevent. Falling through means we probe the default
            # address and report what is actually true: nothing is listening.
            #
            # Only staleness falls through. A malformed or unreadable lease
            # still raises, because that is a real anomaly rather than an
            # ordinary stopped process.
            if exc.code != "stale_lease":
                raise
        else:
            return Target(
                lease["base_url"],
                "lease",
                True,
                lease_service_version=lease["service_version"],
            )
    return Target(spec.default_url, "default", False)


def validate_service_manifest(payload: Any, spec: ServiceSpec) -> dict[str, Any]:
    code = "contract_mismatch"
    payload = _exact(payload, _MANIFEST_KEYS, "service manifest", code=code)
    if (
        payload["ok"] is not True
        or payload["contract"] != "ryan.suite.service"
        or payload["version"] != 1
    ):
        raise _error(code, "service manifest contract does not match version 1")
    service = _exact(payload["service"], _SERVICE_KEYS, "service", code=code)
    if service["id"] != spec.service_id:
        raise _error("wrong_service", f"configured endpoint is not {spec.name}")
    if (
        service["name"] != spec.name
        or not isinstance(service["service_version"], str)
        or not service["service_version"]
        or service["api_version"] != 1
        or service["resident"] is not True
        or service["default_port"] != spec.default_port
    ):
        raise _error(code, f"{spec.name} service identity does not match version 1")
    health = _exact(
        service["health"],
        {"contract", "version", "href"},
        "service health descriptor",
        code=code,
    )
    if health != {
        "contract": "ryan.suite.health",
        "version": 1,
        "href": "/api/suite/v1/health",
    }:
        raise _error(code, f"{spec.name} health descriptor does not match version 1")
    _capabilities(service["capabilities"], spec, code=code)
    _ui(service["ui"], spec, code=code)
    mcp = _exact(
        service["mcp"],
        {"name", "transport"},
        "service MCP descriptor",
        code=code,
    )
    if mcp != {"name": spec.service_id, "transport": "stdio"}:
        raise _error(code, f"{spec.name} MCP identity does not match version 1")
    return payload


def validate_health(payload: Any, spec: ServiceSpec) -> dict[str, Any]:
    code = "contract_mismatch"
    payload = _exact(payload, _HEALTH_KEYS, "suite health", code=code)
    if (
        payload["contract"] != "ryan.suite.health"
        or payload["version"] != 1
        or payload["service_id"] != spec.service_id
        or not isinstance(payload["service_version"], str)
        or not payload["service_version"]
        or payload["state"] not in _HEALTH_STATES
        or not isinstance(payload["ok"], bool)
    ):
        raise _error(code, f"{spec.name} health identity does not match version 1")
    checks = payload["checks"]
    if not isinstance(checks, list) or len(checks) != len(spec.health_checks):
        raise _error(code, f"{spec.name} health checks do not match version 1")
    statuses: list[str] = []
    for expected_id, check in zip(spec.health_checks, checks):
        check = _exact(check, _CHECK_KEYS, "suite health check", code=code)
        if (
            check["id"] != expected_id
            or check["required"] is not True
            or check["status"] not in _CHECK_STATUSES
        ):
            raise _error(code, f"{spec.name} health checks do not match version 1")
        statuses.append(check["status"])
    expected_ok = "failed" not in statuses
    if not expected_ok:
        expected_state = "needs_attention"
    elif any(status in {"busy", "degraded"} for status in statuses):
        expected_state = "ready_with_limits"
    else:
        expected_state = "ready"
    if payload["ok"] is not expected_ok or payload["state"] != expected_state:
        raise _error(code, f"{spec.name} health state is internally inconsistent")
    return payload


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "ReplayRyan/0.1 local-suite-doctor",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read(_JSON_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        raise _error(
            "contract_mismatch",
            f"suite endpoint returned HTTP {exc.code}",
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise TransportError(
            "timeout",
            "suite endpoint timed out",
            retryable=True,
            absence_eligible=True,
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise TransportError(
                "timeout",
                "suite endpoint timed out",
                retryable=True,
                absence_eligible=True,
            ) from exc
        raise TransportError(
            "unavailable",
            "suite endpoint is unavailable",
            retryable=True,
            absence_eligible=True,
        ) from exc
    if status != 200:
        raise _error("contract_mismatch", f"suite endpoint returned HTTP {status}")
    if len(raw) > _JSON_LIMIT:
        raise _error("contract_mismatch", "suite endpoint response exceeds size limit")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(
            "contract_mismatch",
            "suite endpoint did not return valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise _error("contract_mismatch", "suite endpoint did not return a JSON object")
    return payload


def _peer(
    service_id: str,
    state: str,
    capabilities: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "contract": "ryan.suite.peer",
        "version": 1,
        "peer": service_id,
        "state": state,
        "capabilities": capabilities or [],
    }


def _unhealthy(spec: ServiceSpec, error: PeerError) -> dict[str, Any]:
    return {
        "ok": False,
        "contract": "ryan.suite.peer",
        "version": 1,
        "peer": spec.service_id,
        "state": "unhealthy",
        "error": {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        },
    }


def probe_resident(
    spec: ServiceSpec,
    *,
    explicit_url: str | None = None,
    registry_dir: Path | None = None,
    timeout: float = 1.0,
    include_ui: bool = False,
    pid_checker: Callable[[int], bool] = process_is_live,
    json_fetcher: Callable[[str, float], dict[str, Any]] = fetch_json,
) -> dict[str, Any]:
    try:
        target = resolve_target(
            spec,
            explicit_url=explicit_url,
            registry_dir=registry_dir,
            pid_checker=pid_checker,
        )
    except PeerError as error:
        return {
            "supported": True,
            "running": False,
            "discovery": "explicit" if explicit_url is not None else "lease",
            "health": None,
            "peer": _unhealthy(spec, error),
        }
    try:
        manifest = validate_service_manifest(
            json_fetcher(
                target.base_url + "/.well-known/suite-service.json",
                timeout,
            ),
            spec,
        )
    except TransportError as error:
        if target.source == "default" and error.absence_eligible:
            peer = _peer(spec.service_id, "absent")
        else:
            peer = _unhealthy(spec, error)
        return {
            "supported": True,
            "running": False,
            "discovery": target.source,
            "health": None,
            "peer": peer,
        }
    except PeerError as error:
        return {
            "supported": True,
            "running": False,
            "discovery": target.source,
            "health": None,
            "peer": _unhealthy(spec, error),
        }
    try:
        health = validate_health(
            json_fetcher(
                target.base_url + "/api/suite/v1/health",
                timeout,
            ),
            spec,
        )
    except (PeerError, TransportError) as error:
        return {
            "supported": True,
            "running": True,
            "discovery": target.source,
            "health": None,
            "peer": _unhealthy(spec, error),
        }
    if (
        target.lease_service_version is not None
        and manifest["service"]["service_version"] != target.lease_service_version
    ):
        error = _error(
            "contract_mismatch",
            f"{spec.name} runtime lease and manifest versions disagree",
        )
        return {
            "supported": True,
            "running": True,
            "discovery": target.source,
            "health": None,
            "peer": _unhealthy(spec, error),
        }
    if health["service_version"] != manifest["service"]["service_version"]:
        error = _error(
            "contract_mismatch",
            f"{spec.name} manifest and health versions disagree",
        )
        return {
            "supported": True,
            "running": True,
            "discovery": target.source,
            "health": None,
            "peer": _unhealthy(spec, error),
        }
    health_summary = {
        "ok": health["ok"],
        "state": health["state"],
        "service_version": health["service_version"],
        "checks": [
            {
                "id": item["id"],
                "required": item["required"],
                "status": item["status"],
            }
            for item in health["checks"]
        ],
    }
    if not health["ok"]:
        error = _error(
            "peer_unhealthy",
            f"{spec.name} reports that it needs attention",
            retryable=True,
        )
        peer = _unhealthy(spec, error)
    else:
        # This hub deliberately reads no product credential. Section 3.3
        # therefore requires the detected service to be "unconfigured".
        peer = _peer(spec.service_id, "unconfigured")
    result = {
        "supported": True,
        "running": True,
        "discovery": target.source,
        "health": health_summary,
        "peer": peer,
    }
    if include_ui:
        ui = manifest["service"]["ui"]
        result["ui"] = {
            "home": target.base_url + ui["home"],
            "routes": {
                name: target.base_url + path
                for name, path in ui["routes"].items()
            },
        }
    return result


def _extract_mcp_config(output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for offset, character in enumerate(output):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "mcpServers" in value:
            return value
    raise _error(
        "invalid_configuration",
        "product --print-config output did not contain an MCP configuration",
    )


def validate_print_config(payload: Any, spec: ServiceSpec) -> dict[str, Any]:
    code = "invalid_configuration"
    payload = _exact(payload, {"mcpServers"}, "MCP configuration", code=code)
    servers = _exact(
        payload["mcpServers"],
        {spec.service_id},
        "MCP server registry",
        code=code,
    )
    entry = _exact(
        servers[spec.service_id],
        {"command", "args"},
        "MCP server entry",
        code=code,
    )
    if not isinstance(entry["command"], str) or not entry["command"].strip():
        raise _error(code, "MCP command is missing")
    expected_args = ["-m", str(spec.mcp_module), "serve-mcp"]
    if entry["args"] != expected_args:
        raise _error(code, "MCP arguments do not match the product launcher")
    return payload


def probe_mcp(
    spec: ServiceSpec,
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout: float = 5.0,
) -> dict[str, Any]:
    if spec.cli_name is None or spec.mcp_module is None:
        return {
            "supported": True,
            "transport": "stdio",
            "launchable": None,
            "discovery": "not_exposed",
            "state": "unknown",
        }
    executable = which(spec.cli_name)
    if executable is None:
        return {
            "supported": True,
            "transport": "stdio",
            "launchable": False,
            "discovery": "path",
            "state": "absent",
        }
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    try:
        result = runner(
            [executable, *spec.print_config_args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        error = _error(
            "unavailable",
            f"{spec.name} --print-config could not complete",
            retryable=True,
        )
        return {
            "supported": True,
            "transport": "stdio",
            "launchable": False,
            "discovery": "print_config",
            "state": "unhealthy",
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            },
        }
    if result.returncode != 0:
        error = _error(
            "unavailable",
            f"{spec.name} --print-config exited with an error",
            retryable=True,
        )
        return {
            "supported": True,
            "transport": "stdio",
            "launchable": False,
            "discovery": "print_config",
            "state": "unhealthy",
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            },
        }
    try:
        validate_print_config(_extract_mcp_config(result.stdout), spec)
    except PeerError as error:
        return {
            "supported": True,
            "transport": "stdio",
            "launchable": False,
            "discovery": "print_config",
            "state": "unhealthy",
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            },
        }
    return {
        "supported": True,
        "transport": "stdio",
        "launchable": True,
        "discovery": "print_config",
        "state": "available",
    }
