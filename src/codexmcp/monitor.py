"""Local web GUI for watching codex runs recorded by the MCP server.

Runs as a standalone process (``codexmcp-monitor``) that only ever *reads* the
files under ``~/.codexmcp/runs``. It is fully decoupled from the MCP server:
starting or stopping the monitor never affects an in-flight codex task.

Clients poll ``/api/runs`` for the list and ``/api/runs/{id}/events?offset=N``
for new events, so a dropped request self-heals on the next tick.
"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path
from typing import Any, Dict

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from codexmcp import recorder

_INDEX = Path(__file__).parent / "static" / "index.html"


async def index(request: Request) -> HTMLResponse:
    try:
        return HTMLResponse(_INDEX.read_text(encoding="utf-8"))
    except OSError:
        return HTMLResponse("<h1>monitor UI asset is missing</h1>", status_code=500)


async def api_runs(request: Request) -> JSONResponse:
    runs = recorder.list_runs()
    # Keep the payload small: the list view only needs a summary per run.
    summaries = [
        {
            key: run.get(key)
            for key in (
                "run_id",
                "status",
                "started_at",
                "updated_at",
                "finished_at",
                "session_id",
                "event_count",
                "cd",
                "sandbox",
                "model",
            )
        }
        | {"prompt": (run.get("prompt") or "")[:300]}
        for run in runs
    ]
    return JSONResponse({"runs": summaries, "now": recorder.utcnow()})


async def api_run_detail(request: Request) -> JSONResponse:
    run = recorder.get_run(request.path_params["run_id"])
    if run is None:
        return JSONResponse({"error": "run not found"}, status_code=404)
    return JSONResponse(run)


async def api_run_events(request: Request) -> JSONResponse:
    run_id = request.path_params["run_id"]
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
    except ValueError:
        offset = 0
    payload: Dict[str, Any] = recorder.read_events(run_id, offset)
    payload["meta"] = recorder.get_run(run_id)
    return JSONResponse(payload)


def build_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/runs", api_runs),
            Route("/api/runs/{run_id}", api_run_detail),
            Route("/api/runs/{run_id}/events", api_run_events),
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codexmcp-monitor",
        description="Watch live and past codex runs recorded by the Codex MCP server.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser on start")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"codexmcp monitor -> {url}")
    print(f"reading runs from {recorder.runs_dir()}")
    if not args.no_open:
        webbrowser.open(url)

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
