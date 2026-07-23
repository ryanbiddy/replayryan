"""Build, install, and exercise Thrum from a fresh temporary environment."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=ROOT, env=env, check=True)


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
    print("clean-install: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
