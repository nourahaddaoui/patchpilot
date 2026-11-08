"""Call one tool by hand and log it.

    .venv/bin/python -m agent.tools read_file '{"path": "requirements.txt"}'
    .venv/bin/python -m agent.tools run_scan '{}'
    .venv/bin/python -m agent.tools run_tests '{}' --repo sandbox/template
"""

from __future__ import annotations

import argparse
import json
import sys

from agent.calllog import CallLogger
from agent.tools.context import PROJECT_ROOT, ToolContext
from agent.tools.registry import TOOLS, call_tool


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent.tools")
    parser.add_argument("tool", choices=sorted(TOOLS))
    parser.add_argument("args", nargs="?", default="{}", help="JSON object of tool arguments")
    parser.add_argument("--repo", default=str(PROJECT_ROOT / "sandbox" / "template"))
    ns = parser.parse_args(argv)
    try:
        args = json.loads(ns.args)
    except json.JSONDecodeError as e:
        print(f"args is not valid JSON: {e}", file=sys.stderr)
        return 2
    logger = CallLogger()
    result = call_tool(ns.tool, args, ToolContext(repo_root=ns.repo), logger, step=1)
    print(result.content)
    print(f"\n[ok={result.ok} {result.duration_ms}ms] logged to {logger.path}", file=sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
