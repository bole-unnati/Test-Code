"""
src/api/server.py
FastAPI server with live log streaming to frontend via WebSocket.

WebSocket message types:
  init         — current state on connect
  phase_update — phase completed (has events + state snapshot)
  log          — single log line from agent (streams live as it runs)
  terminal     — pipeline finished or failed
  ping         — keepalive
"""
from __future__ import annotations
import os, sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import asyncio, json, uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional, Callable

import structlog
from fastapi import FastAPI, UploadFile, File, Form, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from src.graph.workflow import run_pipeline, run_single_phase, PHASES

log = structlog.get_logger(__name__)

# ── In-memory stores ──────────────────────────────────────────────────────────
_runs:       dict[str, dict]         = {}
_ws_clients: dict[str, list[WebSocket]] = {}
_tasks:      dict[str, asyncio.Task] = {}

# ── Global log broadcaster — base.py calls this for every log line ────────────
# Maps run_id → async callable(level, phase, message)
_log_callbacks: dict[str, Callable] = {}


def register_log_callback(run_id: str, cb: Callable):
    _log_callbacks[run_id] = cb

def unregister_log_callback(run_id: str):
    _log_callbacks.pop(run_id, None)


async def _broadcast(run_id: str, message: dict):
    dead = []
    for ws in _ws_clients.get(run_id, []):
        try:
            await ws.send_text(json.dumps(message, default=str))
        except Exception:
            dead.append(ws)
    for ws in dead:
        try: _ws_clients[run_id].remove(ws)
        except ValueError: pass


# ── Pipeline task ─────────────────────────────────────────────────────────────

async def _run_pipeline_task(run_id: str, raw_doc: str, project_name: str):
    _runs[run_id]["status"] = "running"

    # Register a log callback so base.py can send lines to the frontend
    async def send_log(level: str, phase: str, message: str, elapsed: float = 0):
        _runs[run_id]["logs"].append({
            "level": level, "phase": phase,
            "message": message, "elapsed": elapsed,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        await _broadcast(run_id, {
            "type":    "log",
            "run_id":  run_id,
            "level":   level,
            "phase":   phase,
            "message": message,
            "elapsed": elapsed,
            "ts":      datetime.now(timezone.utc).isoformat(),
        })

    register_log_callback(run_id, send_log)

    async def on_event(patch: dict):
        _runs[run_id]["state"].update(patch.get("state_snapshot", {}))
        _runs[run_id]["events"].extend(patch.get("events", []))
        await _broadcast(run_id, {
            "type":       "phase_update",
            "run_id":     run_id,
            "node":       patch.get("node"),
            "phase":      patch.get("phase"),
            "next_phase": patch.get("next_phase"),
            "events":     patch.get("events", []),
            "state":      patch.get("state_snapshot", {}),
            "ts":         datetime.now(timezone.utc).isoformat(),
        })

    try:
        final = await run_pipeline(raw_doc, project_name, run_id, on_event=on_event)
        status = "done" if final.get("current_phase") == "done" else "failed"
        _runs[run_id]["status"] = status
        _runs[run_id]["final_state"] = {
            k: v for k, v in final.items()
            if k not in ("messages", "raw_requirements_doc")
        }
        _runs[run_id]["state"].update(_runs[run_id]["final_state"])
        await _broadcast(run_id, {
            "type":        "terminal",
            "run_id":      run_id,
            "status":      status,
            "final_state": _runs[run_id]["final_state"],
            "ts":          datetime.now(timezone.utc).isoformat(),
        })
    except asyncio.CancelledError:
        _runs[run_id]["status"] = "aborted"
        await _broadcast(run_id, {"type":"terminal","run_id":run_id,"status":"aborted"})
    except Exception as e:
        log.error("pipeline.error", run_id=run_id, error=str(e))
        _runs[run_id]["status"] = "error"
        _runs[run_id]["error"]  = str(e)
        await _broadcast(run_id, {"type":"terminal","run_id":run_id,"status":"error","error":str(e)})
    finally:
        unregister_log_callback(run_id)


# ── FastAPI ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app):
    # Inject broadcaster into base.py so every LiveLog line goes to the frontend
    import src.agents.base as _base
    _base._GLOBAL_LOG_CALLBACK = _log_callbacks
    log.info("api.startup", model=getattr(settings,"groq_model","?"),
             services=settings.enabled_services)
    yield
    for task in _tasks.values():
        task.cancel()

app = FastAPI(
    title="SDLC Automation Platform — OSS Edition",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok",
            "groq_model":  getattr(settings, "groq_model",  "?"),
            "ollama_model": getattr(settings, "ollama_model", "?"),
            "services":    settings.enabled_services,
            "ts":          datetime.now(timezone.utc).isoformat()}

@app.get("/api/services")
async def service_status():
    all_svc = ["gitea","plane","outline","penpot","mattermost","minio","zammad"]
    enabled = settings.enabled_services
    return {s: ("enabled" if s in enabled else "disabled") for s in all_svc}

@app.post("/api/runs")
async def start_run(
    file: Optional[UploadFile] = File(default=None),
    project_name: str = Form(...),
    requirements_text: Optional[str] = Form(default=None),
):
    if file and file.filename:
        content = await file.read()
        raw_doc = content.decode("utf-8", errors="replace")
    elif requirements_text:
        raw_doc = requirements_text
    else:
        raise HTTPException(400, "Provide a file or requirements_text")

    run_id = str(uuid.uuid4())
    now    = datetime.now(timezone.utc).isoformat()

    _runs[run_id] = {
        "run_id":       run_id,
        "project_name": project_name,
        "raw_doc":       raw_doc,
        "status":       "queued",
        "state":        {"current_phase": "init", "requirements_count": 0,
                         "work_items_count": 0, "test_coverage": 0.0},
        "events":       [],
        "logs":         [],    # ← live log lines stored here
        "final_state":  None,
        "error":        None,
        "created_at":   now,
    }

    task = asyncio.create_task(_run_pipeline_task(run_id, raw_doc, project_name))
    _tasks[run_id] = task
    log.info("run.started", run_id=run_id, project=project_name)

    return {"run_id": run_id, "status": "started",
            "project_name": project_name, "ws_url": f"/ws/{run_id}"}

@app.get("/api/runs")
async def list_runs():
    return [{"run_id":       r["run_id"],
             "project_name": r["project_name"],
             "status":       r["status"],
             "created_at":   r["created_at"],
             "current_phase":r["state"].get("current_phase","init")}
            for r in _runs.values()]

@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run = _runs.get(run_id)
    if not run:
        raise HTTPException(404, f"Run {run_id} not found")
    return run

@app.post("/api/runs/{run_id}/abort")
async def abort_run(run_id: str):
    task = _tasks.get(run_id)
    if task:
        task.cancel()
        _runs[run_id]["status"] = "aborted"
    return {"run_id": run_id, "status": "aborted"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

# ── Phase re-run ─────────────────────────────────────────────────────────────

async def _run_phase_task(run_id: str, phase: str):
    """Background task: re-run one phase using the saved state of a previous run."""
    run = _runs.get(run_id)
    if not run:
        return

    # Mark as running again
    run["status"] = "running"
    run["logs"].append({
        "level": "info", "phase": phase,
        "message": f"Re-running phase: {phase}", "elapsed": 0,
    })

    async def send_log(level: str, ph: str, message: str, elapsed: float = 0):
        entry = {"level": level, "phase": ph, "message": message, "elapsed": elapsed}
        run["logs"].append(entry)
        await _broadcast(run_id, {"type": "log", "run_id": run_id, **entry})

    register_log_callback(run_id, send_log)

    async def on_event(patch: dict):
        run["state"].update(patch.get("state_snapshot", {}))
        run["events"].extend(patch.get("events", []))
        await _broadcast(run_id, {
            "type":        "phase_update",
            "run_id":      run_id,
            "rerun":       True,
            "phase":       patch.get("phase"),
            "events":      patch.get("events", []),
            "state":       patch.get("state_snapshot", {}),
        })

    try:
        # Build state from what we saved plus the original doc
        saved   = run.get("state", {})
        raw_doc = run.get("raw_doc", "")

        # Reconstruct a full SDLCState from saved + initial defaults
        from src.graph.state import initial_state
        base = initial_state(raw_doc, saved.get("project_name",""), run_id)
        merged_state = {**base, **saved}   # saved fields win over defaults

        new_state = await run_single_phase(phase, merged_state, run_id, on_event=on_event)

        # Persist the updated fields back
        run["state"].update({
            k: v for k, v in new_state.items()
            if k not in ("run_id", "raw_doc")
        })
        run["status"] = "done"

        await _broadcast(run_id, {
            "type":    "phase_rerun_complete",
            "run_id":  run_id,
            "phase":   phase,
            "status":  "done",
            "state":   run["state"],
        })
        await send_log("success", phase, f"Phase {phase} re-run complete", 0)

    except Exception as e:
        run["status"] = "failed"
        await send_log("error", phase, f"Phase re-run failed: {e}", 0)
        await _broadcast(run_id, {
            "type":   "phase_rerun_complete",
            "run_id": run_id,
            "phase":  phase,
            "status": "failed",
            "error":  str(e),
        })
    finally:
        unregister_log_callback(run_id)


@app.post("/api/runs/{run_id}/phase/{phase}")
async def rerun_phase(run_id: str, phase: str, background_tasks: BackgroundTasks):
    """
    Re-run a single pipeline phase for an existing run.
    Uses the saved state from the previous run — no need to re-upload the doc.
    The phase result is merged back into the run state.
    """
    run = _runs.get(run_id)
    if not run:
        from fastapi import HTTPException
        raise HTTPException(404, f"Run {run_id} not found")

    if phase not in PHASES:
        from fastapi import HTTPException
        raise HTTPException(400, f"Unknown phase '{phase}'. Valid: {PHASES}")

    if run.get("status") == "running":
        from fastapi import HTTPException
        raise HTTPException(409, "A run is already in progress. Wait for it to complete.")

    background_tasks.add_task(_run_phase_task, run_id, phase)
    return {
        "run_id": run_id,
        "phase":  phase,
        "status": "started",
        "message": f"Re-running phase '{phase}' — connect to /ws/{run_id} for live logs",
    }


@app.get("/api/phases")
async def list_phases():
    """Return the list of valid pipeline phases."""
    return {"phases": PHASES}


@app.websocket("/ws/{run_id}")
async def ws_endpoint(websocket: WebSocket, run_id: str):
    await websocket.accept()
    _ws_clients.setdefault(run_id, []).append(websocket)
    log.info("ws.connect", run_id=run_id)

    try:
        run = _runs.get(run_id)
        if run:
            # Send current state + all buffered logs immediately on connect
            await websocket.send_text(json.dumps({
                "type":   "init",
                "run_id": run_id,
                "status": run["status"],
                "state":  run["state"],
                "events": run["events"],
                "logs":   run["logs"],    # ← buffered logs since run started
            }, default=str))

        while True:
            await asyncio.sleep(30)
            await websocket.send_text(json.dumps({"type": "ping"}))

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try: _ws_clients[run_id].remove(websocket)
        except (ValueError, KeyError): pass
        log.info("ws.disconnect", run_id=run_id)