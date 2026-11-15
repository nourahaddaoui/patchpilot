"""One setup command, any OS:  python scripts/setup.py

1. .venv            the agent's interpreter (requirements.txt)
2. sandbox/.venv    the interpreter the template repo's tests run in (pytest)
3. Trivy DB         downloaded once into the cache so scans are offline and reproducible
                    ($PATCHPILOT_TRIVY_CACHE, default .cache/trivy). --refresh-db re-downloads it.

Safe to run again: existing venvs and an existing DB are kept.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTEST = "pytest==8.3.3"


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sh(*cmd, **kw) -> None:
    print("  $", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True, **kw)


def make_venv(venv: Path, packages: list[str]) -> Path:
    py = venv_python(venv)
    if not py.is_file():
        sh(sys.executable, "-m", "venv", venv)
    sh(py, "-m", "pip", "install", "-q", "--disable-pip-version-check", *packages)
    return py


def trivy_cache() -> Path:
    return Path(os.environ.get("PATCHPILOT_TRIVY_CACHE") or ROOT / ".cache" / "trivy")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/setup.py")
    parser.add_argument("--refresh-db", action="store_true", help="download the Trivy DB again")
    ns = parser.parse_args(argv)

    if sys.version_info < (3, 10):
        print(f"PatchPilot needs Python 3.10+, this is {sys.version.split()[0]}.", file=sys.stderr)
        return 1

    print("[1/3] agent interpreter (.venv)")
    make_venv(ROOT / ".venv", ["-r", str(ROOT / "requirements.txt")])

    print("[2/3] template test interpreter (sandbox/.venv)")
    make_venv(ROOT / "sandbox" / ".venv", [PYTEST])

    print("[3/3] Trivy vulnerability DB")
    if shutil.which("trivy") is None:
        print("Trivy is not installed. Install it, then run this again:\n"
              "  Windows: winget install AquaSecurity.Trivy\n"
              "  macOS:   brew install trivy\n"
              "  Linux:   https://trivy.dev/latest/getting-started/installation/", file=sys.stderr)
        return 1
    cache = trivy_cache()
    if (cache / "db" / "trivy.db").is_file() and not ns.refresh_db:
        print(f"  DB already cached in {cache} (use --refresh-db to update it)")
    else:
        cache.mkdir(parents=True, exist_ok=True)
        sh("trivy", "fs", "--cache-dir", cache, "--download-db-only")

    print("\nReady. Run the agent with:\n  python run.py\n"
          "or on a real repo:\n  python run.py --target https://github.com/OWNER/REPO\n"
          "Run the tests with:\n  python run.py test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
