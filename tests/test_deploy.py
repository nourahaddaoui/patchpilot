"""The deployment files: what goes into the image, and how it runs.

Building the image needs Docker, so these tests read the files instead; the image itself
was built and run (health check, non-root user, a full browser run) before it was committed.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def lines(name: str) -> list[str]:
    return [line.strip() for line in (ROOT / name).read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")]


def test_secrets_and_local_state_never_go_into_the_image():
    ignored = lines(".dockerignore")
    for entry in (".git", ".env", ".env.*", ".venv", "sandbox/work", "logs/*.jsonl", "eval/runs"):
        assert entry in ignored, entry
    assert "!.env.example" in ignored  # the example has no values


def test_the_image_runs_the_web_demo_as_a_non_root_user():
    docker = lines("Dockerfile")
    users = [line for line in docker if line.startswith("USER ")]
    assert users and users[-1] != "USER root"
    assert docker[-1] == 'CMD ["python", "-m", "web"]'
    assert any(line.startswith("HEALTHCHECK") for line in docker)
    assert any("pip install -r requirements.txt" in line for line in docker)


def test_tracing_is_off_in_the_image():
    assert any("LANGSMITH_TRACING=false" in line for line in lines("Dockerfile"))


def test_render_blueprint_builds_the_dockerfile_with_a_health_check():
    import yaml

    spec = yaml.safe_load((ROOT / "render.yaml").read_text())
    (svc,) = spec["services"]
    assert svc["type"] == "web" and svc["runtime"] == "docker"
    assert svc["dockerfilePath"] == "./Dockerfile" and (ROOT / "Dockerfile").is_file()
    assert svc["healthCheckPath"] == "/healthz"
    env = {e["key"]: e.get("value") for e in svc.get("envVars", [])}
    assert not any(k in env for k in ("OPENAI_API_KEY", "GITHUB_TOKEN", "LANGSMITH_API_KEY"))  # secrets live in the dashboard
    assert "PATCHPILOT_WEB_LLM_MODELS" not in env  # no paid model on the public demo by default


def test_the_health_check_path_is_served():
    from fastapi.testclient import TestClient

    from web.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
