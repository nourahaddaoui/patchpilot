"""scripts/scan_secrets.py: the credential scan run before the repo is frozen.

Fake keys are assembled at run time, so this file never contains one the scanner would flag.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scan_secrets", ROOT / "scripts" / "scan_secrets.py")
scan = importlib.util.module_from_spec(spec)
sys.modules["scan_secrets"] = scan  # dataclasses look their module up here
spec.loader.exec_module(scan)

RAND = "Zq7Lm2Vp9Rt4Kx8Nw3Hs6Jd1Fb5Gc0Ya"  # 32 characters with no placeholder word in them

FAKES = {
    "GitHub token": "gh" + "p_" + RAND + "Ab3D",  # real ones have 36 characters after ghp_
    "OpenAI key": "s" + "k-proj-" + RAND + "_q1",
    "LangSmith key": "lsv2" + "_pt_" + RAND,
    "AWS access key": "AK" + "IA" + "Q7LM2VP9RT4KX8NW",
    "Private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "Hard-coded secret": 'api_key = "' + RAND + '"',
}


@pytest.mark.parametrize("kind", list(FAKES))
def test_each_kind_of_credential_is_found(kind):
    found = scan.scan_text(f"line one\nvalue: {FAKES[kind]}\n", "f.py")
    assert [(f.kind, f.line) for f in found] == [(kind, 2)]


def test_findings_are_masked_and_never_printed_in_full(tmp_path, capsys):
    secret_file = tmp_path / "config.py"
    secret_file.write_text(f"TOKEN = '{FAKES['GitHub token']}'\n")
    assert scan.main([str(secret_file)]) == 1
    out = capsys.readouterr().out
    assert "GitHub token" in out and FAKES["GitHub token"] not in out and RAND not in out


@pytest.mark.parametrize("line", [
    'api_key = "your-api-key-here-0000000"',
    "GITHUB_TOKEN=ghp_" + "x" * 36,
    'token = "sk-test-' + "0" * 32 + '"',
    "real-looking " + FAKES["OpenAI key"] + "  # scan-secrets: allow",
])
def test_placeholders_and_allowed_lines_are_not_findings(line):
    assert scan.scan_text(line) == []


def test_a_committed_env_file_is_a_finding_but_the_example_is_not(tmp_path):
    (tmp_path / ".env").write_text("NOTHING_SECRET_HERE=1\n")
    (tmp_path / ".env.example").write_text("OPENAI_API_KEY=\n")
    found = scan.scan_paths([tmp_path], root=tmp_path)
    assert [(f.path, f.kind) for f in found] == [(".env", "Committed .env file")]


def test_this_repo_has_no_credentials_in_any_tracked_file(capsys):
    assert scan.main([]) == 0, capsys.readouterr().out
