"""edit_file (write): set one package's exact pin in requirements.txt.

Safety rules:
- Only requirements.txt at the repo root can be edited (editable-path allowlist).
- Only inside a sandbox created by sandbox/reset.sh, never the template.
- Idempotent: if the pin is already at the target version nothing is written
  and no approval is asked. Same call twice changes nothing.
- Never downgrades an exact pin.
- Nothing is written until ctx.approve(proposal) returns True. The proposal
  carries the full unified diff the human sees.
"""

from __future__ import annotations

import difflib
import json
import re

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from agent.sandbox import is_sandbox
from agent.tools._paths import resolve_in_repo
from agent.tools.context import ToolContext
from agent.tools.errors import ToolError

NAME = "edit_file"
DESCRIPTION = (
    "Set the exact version pin of one package in requirements.txt (the only editable file). "
    "Bumps an existing pin, or adds a pin for a package not listed yet (e.g. a transitive "
    "dependency). Never downgrades. A human must approve the diff before anything is written; "
    "if they deny it, nothing changes and the run must stop. Calling it again with the same "
    "arguments changes nothing. Returns JSON: action, changed, from, to, diff."
)
INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "File to edit. Must be 'requirements.txt'.",
        },
        "package": {
            "type": "string",
            "minLength": 1,
            "maxLength": 100,
            "pattern": r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?",
            "description": "Package name as on PyPI, e.g. 'requests'.",
        },
        "version": {
            "type": "string",
            "minLength": 1,
            "maxLength": 50,
            "pattern": r"[0-9][0-9A-Za-z.+!-]*",
            "description": "Exact version to pin, e.g. '2.32.4'.",
        },
    },
    "required": ["path", "package", "version"],
    "additionalProperties": False,
}

EDITABLE = ("requirements.txt",)

_REQ = re.compile(
    r"^(?P<lead>\s*)"
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?P<extras>\s*\[[^\]]*\])?"
    r"(?P<spec>\s*(?:===|==|~=|!=|<=|>=|<|>)[^;#]*?)?"
    r"(?P<tail>\s*(?:;[^#]*?)?\s*(?:#.*)?)$"
)
_NAME = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def _line_name(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "-")):
        return None
    m = _NAME.match(line)
    return canonicalize_name(m.group(1)) if m else None


def plan_edit(text: str, package: str, version: str) -> dict:
    """Pure function: compute the new requirements.txt text. Raises ToolError."""
    try:
        target = Version(version)
    except InvalidVersion:
        raise ToolError("bad_arguments", f"'{version}' is not a valid version number.") from None
    if str(target) != version:
        raise ToolError("bad_arguments", f"'{version}' is not in normal form; use '{target}'.")

    want = canonicalize_name(package)
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if _line_name(line) == want]
    if len(hits) > 1:
        raise ToolError("not_allowed", f"'{package}' appears on {len(hits)} lines of requirements.txt; refusing an ambiguous edit.")

    if not hits:
        prefix = "" if not text or text.endswith("\n") else "\n"
        new_text = text + prefix + f"{package}=={version}\n"
        return {"action": "added", "from": None, "to": version, "new_text": new_text}

    i = hits[0]
    line = lines[i]
    body = line.rstrip("\r\n")
    ending = line[len(body):]
    if body.rstrip().endswith("\\") or "--hash" in body:
        raise ToolError("not_allowed", f"the '{package}' line uses hashes or a line continuation; it cannot be edited safely.")
    m = _REQ.match(body)
    if not m:
        raise ToolError("not_allowed", f"the '{package}' line is not a simple version pin ({body.strip()[:80]}); it cannot be edited safely.")

    spec = (m.group("spec") or "").strip()
    current = None
    if spec.startswith("==") and not spec.startswith("==="):
        current_raw = spec[2:].strip()
        if "," not in current_raw and "*" not in current_raw:
            try:
                current = Version(current_raw)
            except InvalidVersion:
                current = None
    if current is not None:
        if current == target:
            return {"action": "unchanged", "from": str(current_raw), "to": version, "new_text": text}
        if target < current:
            raise ToolError(
                "not_allowed",
                f"'{package}' is pinned at {current_raw}; {version} would be a downgrade. Pick a version >= {current_raw}.",
            )

    new_body = f"{m.group('lead')}{m.group('name')}{(m.group('extras') or '').strip()}=={version}{m.group('tail')}"
    lines[i] = new_body + ending
    return {
        "action": "bumped" if current is not None else "pinned",
        "from": spec or None,
        "to": version,
        "new_text": "".join(lines),
    }


def unified_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def run(args: dict, ctx: ToolContext) -> str:
    raw, package, version = args["path"], args["package"], args["version"]
    target = resolve_in_repo(ctx.repo_root, raw, NAME)
    rel = target.relative_to(ctx.repo_root).as_posix()
    if rel not in EDITABLE:
        raise ToolError("not_allowed", f"'{raw}' is not editable. The only file edit_file can change is requirements.txt.")
    if not is_sandbox(ctx.repo_root):
        raise ToolError("not_allowed", "the target repo is not a sandbox clone; edits are only allowed in sandbox/work. Nothing was written.")
    if not target.is_file():
        raise ToolError("not_found", "requirements.txt does not exist in the repo.")

    old = target.read_text(encoding="utf-8")
    plan = plan_edit(old, package, version)
    result = {
        "path": rel,
        "package": package,
        "action": plan["action"],
        "from": plan["from"],
        "to": plan["to"],
        "changed": plan["new_text"] != old,
        "diff": unified_diff(old, plan["new_text"], rel),
    }
    if not result["changed"]:
        result["approved"] = None
        return json.dumps(result, ensure_ascii=False)

    if ctx.approve is None:
        raise ToolError("approval_required", "edits need human approval and no approver is connected. Nothing was written.")
    proposal = {k: result[k] for k in ("path", "package", "action", "from", "to", "diff")}
    if not ctx.approve(proposal):
        raise ToolError("approval_denied", "the human denied this edit. Nothing was written. Stop the run.")

    if target.read_text(encoding="utf-8") != old:
        raise ToolError("conflict", "requirements.txt changed while waiting for approval. Nothing was written; read it again.")
    target.write_text(plan["new_text"], encoding="utf-8")
    result["approved"] = True
    return json.dumps(result, ensure_ascii=False)
