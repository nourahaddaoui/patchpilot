import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client import build_request, timeout, user_agent  # noqa: E402


def test_build_request_joins_url_and_params():
    req = build_request("/items", {"page": 2})
    assert req.method == "GET"
    assert req.url == "https://api.example.com/v1/items?page=2"


def test_user_agent_prefix():
    assert user_agent().startswith("patchpilot-template/")
    assert build_request("items").headers["User-Agent"] == user_agent()


def test_timeout_is_10():
    assert timeout() == 10
