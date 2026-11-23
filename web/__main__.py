"""python -m web  ->  the demo on http://localhost:8000 (Render sets $PORT)."""

import os

import uvicorn


def main() -> None:
    uvicorn.run("web.app:app", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*", log_level="info")


if __name__ == "__main__":
    main()
