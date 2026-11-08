"""Tiny API client. Builds requests but never sends them (no network in tests)."""

from __future__ import annotations

from urllib.parse import urljoin

import requests

from config import BASE_URL, DEFAULT_TIMEOUT


def user_agent() -> str:
    return f"patchpilot-template/1.0 python-requests/{requests.__version__}"


def timeout() -> int:
    return DEFAULT_TIMEOUT


def build_request(path: str, params: dict | None = None) -> requests.PreparedRequest:
    """Return a prepared GET request for BASE_URL + path. No network call."""
    url = urljoin(BASE_URL, path.lstrip("/"))
    req = requests.Request(
        "GET", url, params=params or {}, headers={"User-Agent": user_agent()}
    )
    return req.prepare()
