"""scripts/reset_demo_repo.py against a fake GitHub: protection always comes back."""

import copy
import importlib.util

import pytest

from agent.tools import create_issue
from tests.conftest import PROJECT_ROOT
from tests.test_open_pr import FakeGitHub

spec = importlib.util.spec_from_file_location("reset_demo_repo", PROJECT_ROOT / "scripts" / "reset_demo_repo.py")
reset_demo_repo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_demo_repo)

R = "/repos/o/r"
ADMIN = "ghp_admin_secret_value"

# Shape of GET /branches/main/protection for: PR required, status check "test" required, no force push.
PROTECTION = {
    "url": "u",
    "required_status_checks": {"url": "u", "strict": True, "contexts": ["test"],
                               "checks": [{"context": "test", "app_id": 15368}], "contexts_url": "u"},
    "required_pull_request_reviews": {"url": "u", "dismiss_stale_reviews": False, "require_code_owner_reviews": False,
                                      "require_last_push_approval": False, "required_approving_review_count": 0},
    "required_signatures": {"url": "u", "enabled": False},
    "enforce_admins": {"url": "u", "enabled": False},
    "required_linear_history": {"enabled": False},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
    "block_creations": {"enabled": False},
    "required_conversation_resolution": {"enabled": False},
    "lock_branch": {"enabled": False},
    "allow_fork_syncing": {"enabled": False},
}
FOOTER_BODY = "x\n\n---\nFiled by " + create_issue.MARK + " after human approval."


def routes(main_sha="merged1", patch_main=(200, {}), read_back=None, fix_branch=True, protected=True):
    return {
        ("GET", f"{R}/issues"): (200, [
            {"number": 3, "title": "Vulnerable dependencies", "body": FOOTER_BODY},
            {"number": 12, "title": "PatchPilot: fix", "body": FOOTER_BODY, "pull_request": {}},
            {"number": 4, "title": "someone else's issue", "body": "not ours"},
        ]),
        ("PATCH", f"{R}/issues/3"): (200, {}),
        ("PATCH", f"{R}/pulls/12"): (200, {}),
        ("GET", f"{R}/git/ref/heads/patchpilot/fix"): (200, {}) if fix_branch else (404, None),
        ("DELETE", f"{R}/git/refs/heads/patchpilot/fix"): (204, None),
        ("GET", f"{R}/git/ref/tags/baseline"): (200, {"object": {"type": "tag", "sha": "tagobj"}}),
        ("GET", f"{R}/git/tags/tagobj"): (200, {"object": {"type": "commit", "sha": "base000"}}),
        ("GET", f"{R}/git/ref/heads/main"): (200, {"object": {"sha": main_sha}}),
        ("GET", f"{R}/branches/main/protection"): Reads(PROTECTION if protected else None, read_back),
        ("DELETE", f"{R}/branches/main/protection"): (204, None),
        ("PATCH", f"{R}/git/refs/heads/main"): patch_main,
        ("PUT", f"{R}/branches/main/protection"): (200, {}),
    }


class Reads:
    """First GET returns the saved protection; later GETs (the read-back) return read_back or the same."""

    def __init__(self, first, later=None):
        self.first, self.later, self.n = first, later, 0

    def __call__(self, body):
        self.n += 1
        if self.first is None:
            return 404, {"message": "Branch not protected"}
        return 200, copy.deepcopy(self.first if self.n == 1 or self.later is None else self.later)


@pytest.fixture
def envs(tmp_path):
    admin, agent = tmp_path / ".env.admin", tmp_path / ".env"
    admin.write_text(f"PATCHPILOT_ADMIN_TOKEN={ADMIN}\n")
    agent.write_text("PATCHPILOT_GITHUB_REPO=o/r\nPATCHPILOT_ADMIN_TOKEN=from_dot_env_must_be_ignored\n")
    return admin, agent


def run_main(gh, envs, *argv):
    admin, agent = envs
    return reset_demo_repo.main(list(argv), http=gh, admin_env=admin, agent_env=agent)


def test_full_reset_closes_ours_deletes_branch_and_restores_exact_protection(envs, capsys):
    gh = FakeGitHub(routes())
    assert run_main(gh, envs) == 0
    writes = [(c["method"], c["path"]) for c in gh.writes()]
    assert writes == [
        ("PATCH", f"{R}/issues/3"), ("PATCH", f"{R}/pulls/12"),
        ("DELETE", f"{R}/git/refs/heads/patchpilot/fix"),
        ("DELETE", f"{R}/branches/main/protection"),
        ("PATCH", f"{R}/git/refs/heads/main"),
        ("PUT", f"{R}/branches/main/protection"),
    ]
    assert gh.writes()[4]["body"] == {"sha": "base000", "force": True}
    put = gh.writes()[5]["body"]
    assert put == reset_demo_repo.protection_to_put(PROTECTION)
    assert put["required_status_checks"] == {"strict": True, "checks": [{"context": "test", "app_id": 15368}]}
    assert put["required_pull_request_reviews"]["required_approving_review_count"] == 0
    assert put["allow_force_pushes"] is False and put["enforce_admins"] is False and put["restrictions"] is None
    assert all(c["headers"]["Authorization"] == f"Bearer {ADMIN}" for c in gh.calls)
    assert ADMIN not in capsys.readouterr().out


def test_protection_is_restored_even_when_the_force_update_fails(envs, capsys):
    gh = FakeGitHub(routes(patch_main=(422, {"message": "boom"})))
    assert run_main(gh, envs) == 1
    methods = [(c["method"], c["path"]) for c in gh.writes()]
    assert methods[-2:] == [("PATCH", f"{R}/git/refs/heads/main"), ("PUT", f"{R}/branches/main/protection")]
    assert "boom" in capsys.readouterr().err


def test_protection_is_restored_even_when_github_is_unreachable_mid_reset(envs):
    r = routes()
    r[("PATCH", f"{R}/git/refs/heads/main")] = lambda body: (_ for _ in ()).throw(OSError("down"))
    gh = FakeGitHub(r)
    assert run_main(gh, envs) == 1
    assert gh.writes()[-1]["method"] == "PUT"


def test_read_back_mismatch_fails_loudly(envs, capsys):
    changed = copy.deepcopy(PROTECTION)
    changed["allow_force_pushes"] = {"enabled": True}
    gh = FakeGitHub(routes(read_back=changed))
    assert run_main(gh, envs) == 1
    assert "DIFFERENT" in capsys.readouterr().err


def test_dry_run_changes_nothing(envs, capsys):
    gh = FakeGitHub(routes())
    assert run_main(gh, envs, "--dry-run") == 0
    assert gh.writes() == []
    out = capsys.readouterr().out
    assert "would DELETE" in out and "would PATCH" in out and "would PUT" in out and ADMIN not in out


def test_main_already_at_baseline_leaves_protection_alone(envs):
    gh = FakeGitHub(routes(main_sha="base000", fix_branch=False))
    assert run_main(gh, envs) == 0
    assert not any("protection" in c["path"] for c in gh.calls)


def test_unprotected_main_is_reset_without_protection_calls(envs):
    gh = FakeGitHub(routes(protected=False))
    assert run_main(gh, envs) == 0
    assert not any("protection" in c["path"] for c in gh.writes())


def test_admin_token_comes_only_from_env_admin(envs, monkeypatch):
    admin, agent = envs
    monkeypatch.setenv("PATCHPILOT_ADMIN_TOKEN", "from_environment")
    assert reset_demo_repo.admin_token(admin) == ADMIN
    admin.write_text("# nothing here\n")
    gh = FakeGitHub({})
    assert run_main(gh, envs) == 1  # .env and the environment are never used for it
    assert gh.calls == []


def test_mark_matches_the_agent_footer():
    assert reset_demo_repo.MARK == create_issue.MARK
