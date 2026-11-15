"""One run command, any OS. Uses .venv's Python (made by scripts/setup.py).

    python run.py                                   # fix the template repo (free rule-based model)
    python run.py --target https://github.com/o/r   # fix a real repo, cloned into the sandbox
    python run.py --model gpt-4o-mini               # let GPT choose the tools (paid, capped at $0.50)
    python run.py test                              # run the test suite
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main(argv: list[str]) -> int:
    if not VENV_PY.is_file():
        print("Not set up yet. Run:  python scripts/setup.py", file=sys.stderr)
        return 2
    if argv[:1] == ["test"]:
        cmd = [str(VENV_PY), "-m", "pytest", "-q", *argv[1:]]
    else:
        cmd = [str(VENV_PY), "-m", "agent", *argv]
    return subprocess.run(cmd, cwd=ROOT).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
