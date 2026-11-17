"""Cross-platform launcher that honours BIND_HOST / BIND_PORT from .env.

    python run_bridge.py
"""

import os

import uvicorn

import windsor_bridge.config  # noqa: F401  (loads .env)

if __name__ == "__main__":
    uvicorn.run(
        "windsor_bridge.app:app",
        host=os.environ.get("BIND_HOST", "127.0.0.1"),
        port=int(os.environ.get("BIND_PORT", "8000")),
        log_level="info",
    )
