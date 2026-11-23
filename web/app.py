"""HTTP API and page for the hosted demo.

    GET  /healthz                    liveness (Render's health check)
    GET  /                           the page (web/static/index.html)
    GET  /api/scenarios              the scenarios a visitor can run, with the models allowed for each
    POST /api/runs                   {"scenario": id, "model": "rules", "engine": "graph"} -> {"id": ...}
    GET  /api/runs/{id}              status, the step timeline, the question waiting for an answer, the result
    POST /api/runs/{id}/answer       {"approve": true|false} answers the question on screen (once)

Run ids are random and only returned to the browser that started the run. Starts are
rate-limited and capped (RunManager.max_active), every response carries a strict
Content-Security-Policy, and the page renders all agent output as text, never as HTML.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from eval.scenarios import SCENARIOS
from web.runs import RunManager, RunRefused, models_for

STATIC = Path(__file__).resolve().parent / "static"
STARTS_PER_MINUTE = int(os.environ.get("PATCHPILOT_WEB_STARTS_PER_MINUTE", "30"))
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class StartRequest(BaseModel):
    scenario: str = Field(max_length=80)
    model: str = Field(default="rules", max_length=60)
    engine: str = Field(default="graph", max_length=10)


class Answer(BaseModel):
    approve: bool


class StartLimiter:
    """At most `per_minute` run starts in any 60-second window, across all visitors."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.times: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> bool:
        now = time.time()
        with self.lock:
            while self.times and now - self.times[0] > 60:
                self.times.popleft()
            if len(self.times) >= self.per_minute:
                return False
            self.times.append(now)
            return True


def create_app(manager: RunManager | None = None, starts_per_minute: int = STARTS_PER_MINUTE) -> FastAPI:
    runs = manager or RunManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        runs.shutdown()  # any question still open is answered "no"

    app = FastAPI(title="PatchPilot demo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.runs = runs
    limiter = StartLimiter(starts_per_minute)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "active_runs": app.state.runs.active()}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/api/scenarios")
    def scenarios() -> dict:
        return {"scenarios": [
            {"id": s.id, "category": s.category, "description": s.description, "expected_stop": s.expected_stop,
             "models": models_for(s), "github": s.github}
            for s in SCENARIOS
        ]}

    @app.post("/api/runs", status_code=201)
    def start(req: StartRequest) -> dict:
        if not limiter.allow():
            raise HTTPException(429, "too many runs started in the last minute; try again shortly")
        try:
            run = app.state.runs.start(req.scenario, req.model, req.engine)
        except RunRefused as e:
            raise HTTPException(429 if "busy" in str(e) else 400, str(e)) from None
        return {"id": run.id}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        run = app.state.runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run (finished runs are kept for 30 minutes)")
        return run.view()

    @app.post("/api/runs/{run_id}/answer")
    def answer(run_id: str, body: Answer) -> dict:
        if app.state.runs.get(run_id) is None:
            raise HTTPException(404, "no such run")
        if not app.state.runs.answer(run_id, body.approve):
            raise HTTPException(409, "nothing is waiting for an answer")
        return {"ok": True}

    @app.exception_handler(HTTPException)
    async def plain_errors(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=SECURITY_HEADERS)

    return app


app = create_app()
