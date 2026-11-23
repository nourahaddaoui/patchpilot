"""web/app.py: the HTTP API of the hosted demo."""

import shutil
import time

import pytest
from fastapi.testclient import TestClient

from web.app import SECURITY_HEADERS, create_app
from web.runs import RunManager

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


@pytest.fixture
def client():
    manager = RunManager(max_active=2, answer_timeout_s=10)
    with TestClient(create_app(manager, starts_per_minute=5)) as c:
        yield c
    manager.shutdown()


def poll(client, run_id, until, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/api/runs/{run_id}").json()
        if until(view):
            return view
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_health_and_page(client):
    assert client.get("/healthz").json()["ok"] is True
    page = client.get("/")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert '<script src="/static/app.js">' in page.text and "<script>" not in page.text  # no inline script (CSP)
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]


def test_the_page_never_inserts_agent_output_as_html(client):
    js = client.get("/static/app.js").text
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "outerHTML" not in js


def test_every_response_has_the_security_headers(client):
    for r in (client.get("/"), client.get("/api/scenarios"), client.get("/api/runs/nope")):
        for name, value in SECURITY_HEADERS.items():
            assert r.headers[name] == value


def test_scenarios_list_models_per_scenario(client):
    rows = {s["id"]: s for s in client.get("/api/scenarios").json()["scenarios"]}
    assert len(rows) == 40
    assert rows["fix-01-one-cve"]["models"] == ["rules"]
    assert "compromised" in rows["inj-01-scan-title-downgrade"]["models"]


def test_full_run_through_the_api(client):
    run_id = client.post("/api/runs", json={"scenario": "fix-01-one-cve"}).json()["id"]
    view = poll(client, run_id, lambda v: v["status"] == "waiting")
    assert "+requests==" in view["pending"]["text"]
    assert client.post(f"/api/runs/{run_id}/answer", json={"approve": True}).json() == {"ok": True}
    again = client.post(f"/api/runs/{run_id}/answer", json={"approve": True})
    assert again.status_code == 409  # one click, one answer
    view = poll(client, run_id, lambda v: v["status"] == "finished")
    assert view["result"]["stop_reason"] == "done"


@pytest.mark.parametrize("body, status", [
    ({"scenario": "nope"}, 400),
    ({"scenario": "fix-01-one-cve", "model": "gpt-4o-mini"}, 400),
    ({"scenario": "fix-01-one-cve", "engine": "rocket"}, 400),
    ({"scenario": "x" * 81}, 422),
    ({"model": "rules"}, 422),
])
def test_bad_start_requests(client, body, status):
    r = client.post("/api/runs", json=body)
    assert r.status_code == status
    if status == 400:
        assert r.json()["error"]


def test_answer_must_be_a_boolean_and_the_run_must_exist(client):
    assert client.post("/api/runs/nope/answer", json={"approve": True}).status_code == 404
    run_id = client.post("/api/runs", json={"scenario": "fix-01-one-cve"}).json()["id"]
    assert client.post(f"/api/runs/{run_id}/answer", json={"approve": "yes please"}).status_code == 422


def test_start_rate_limit(client):
    codes = [client.post("/api/runs", json={"scenario": "lim-03-never-finishes"}).status_code for _ in range(7)]
    assert 429 in codes and codes.count(201) <= 5


def test_api_docs_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404
