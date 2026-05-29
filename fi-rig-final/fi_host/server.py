"""
fi_host.server — FastAPI backend
Exposes REST + WebSocket endpoints consumed by the web UI and Electron app.

Endpoints:
  GET  /api/ports                    — list serial ports
  POST /api/connect                  — connect to rig
  POST /api/disconnect               — disconnect
  GET  /api/status                   — rig status
  POST /api/reset                    — reset target
  POST /api/glitch                   — single glitch
  POST /api/sweep/start              — start sweep (streams via WS)
  POST /api/sweep/stop               — abort sweep
  GET  /api/sessions                 — list log files
  GET  /api/sessions/{id}/report     — analysis report for a session
  WS   /ws/stream                    — live result stream (JSON frames)
"""
from __future__ import annotations
import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fi_host.core import GlitchParams, SweepParams, GlitchRecord
from fi_host.transport import AsyncRigSerial, RigConnectionError
from fi_host.analysis import ResultLog, generate_report

# ─────────────────────────── APP ────────────────────────────────────────────

app = FastAPI(title="FI Rig API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOG_DIR  = Path(os.environ.get("FI_LOG_DIR", "./fi_logs"))
LOG_DIR.mkdir(exist_ok=True)

# ─────────────────────────── STATE ──────────────────────────────────────────

class AppState:
    rig:            Optional[AsyncRigSerial] = None
    connected:      bool = False
    port:           str  = ""
    sweep_running:  bool = False
    sweep_abort:    bool = False
    active_log:     Optional[ResultLog] = None
    session_id:     str  = ""

state = AppState()
ws_clients: set[WebSocket] = set()


async def broadcast(msg: dict):
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_json(msg)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


# ─────────────────────────── WEBSOCKET ──────────────────────────────────────

@app.websocket("/ws/stream")
async def ws_stream(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    try:
        while True:
            await asyncio.sleep(30)  # keep-alive
    except WebSocketDisconnect:
        ws_clients.discard(ws)


# ─────────────────────────── PORT / CONNECT ─────────────────────────────────

@app.get("/api/ports")
async def list_ports():
    return {"ports": AsyncRigSerial.list_ports()}


class ConnectRequest(BaseModel):
    port: str
    baud: int = 921600


@app.post("/api/connect")
async def connect(req: ConnectRequest):
    if state.connected:
        await state.rig.disconnect()
    state.rig = AsyncRigSerial(req.port, req.baud)
    try:
        ok = await state.rig.connect()
    except RigConnectionError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(400, "Rig did not respond to STATUS")
    state.connected = True
    state.port      = req.port
    await broadcast({"type": "connected", "port": req.port})
    return {"status": "connected", "port": req.port}


@app.post("/api/disconnect")
async def disconnect():
    if state.rig:
        await state.rig.disconnect()
    state.connected = False
    await broadcast({"type": "disconnected"})
    return {"status": "disconnected"}


@app.get("/api/status")
async def rig_status():
    if not state.connected or not state.rig:
        return {"connected": False}
    fw = await state.rig.firmware_status()
    return {
        "connected":     True,
        "port":          state.port,
        "firmware":      fw,
        "sweep_running": state.sweep_running,
        "stats":         state.rig.stats.model_dump(),
    }


@app.post("/api/reset")
async def reset_target():
    _require_connected()
    ok = await state.rig.reset_target()
    await broadcast({"type": "reset", "ok": ok})
    return {"ok": ok}


# ─────────────────────────── GLITCH ─────────────────────────────────────────

@app.post("/api/glitch")
async def single_glitch(params: GlitchParams):
    _require_connected()
    rec = await state.rig.glitch_once(params)
    if rec is None:
        raise HTTPException(504, "No response from rig")
    payload = rec.model_dump()
    await broadcast({"type": "result", "data": payload})
    if state.active_log:
        state.active_log.write(rec)
    return payload


# ─────────────────────────── SWEEP ──────────────────────────────────────────

@app.post("/api/sweep/start")
async def start_sweep(params: SweepParams):
    _require_connected()
    if state.sweep_running:
        raise HTTPException(409, "Sweep already running")

    state.sweep_running = True
    state.sweep_abort   = False
    state.session_id    = datetime.utcnow().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    log_path            = LOG_DIR / f"sweep_{state.session_id}.jsonl"
    state.active_log    = ResultLog(log_path)

    await broadcast({
        "type":    "sweep_start",
        "session": state.session_id,
        "total":   params.total_combinations,
        "params":  params.model_dump(),
    })

    asyncio.create_task(_run_sweep(params))
    return {"session": state.session_id, "total": params.total_combinations}


async def _run_sweep(params: SweepParams):
    count = 0
    try:
        async for rec in state.rig.sweep_stream(params):
            if state.sweep_abort:
                break
            count += 1
            payload = rec.model_dump()
            await broadcast({"type": "result", "data": payload, "n": count})
            if state.active_log:
                state.active_log.write(rec)
    finally:
        if state.active_log:
            state.active_log.close()
            state.active_log = None
        state.sweep_running = False
        await broadcast({"type": "sweep_done", "count": count, "session": state.session_id})


@app.post("/api/sweep/stop")
async def stop_sweep():
    state.sweep_abort = True
    return {"status": "aborting"}


# ─────────────────────────── SESSIONS / ANALYSIS ────────────────────────────

@app.get("/api/sessions")
async def list_sessions():
    files = sorted(LOG_DIR.glob("*.jsonl"), reverse=True)
    sessions = []
    for f in files[:50]:
        stat = f.stat()
        sessions.append({
            "id":       f.stem,
            "filename": f.name,
            "size":     stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        })
    return {"sessions": sessions}


@app.get("/api/sessions/{session_id}/report")
async def session_report(session_id: str):
    path = LOG_DIR / f"{session_id}.jsonl"
    if not path.exists():
        raise HTTPException(404, "Session not found")
    from fi_host.analysis.engine import ResultLog as RL
    records = RL.load(path)
    return generate_report(records)


# ─────────────────────────── STATIC (web UI) ────────────────────────────────

# Support multiple layouts:
#   fi-rig-final/web/          (packaged release)
#   fi-rig-final/web/dist/     (built from source)
#   FI_WEB_ROOT env var        (Electron / custom)
def _find_static_dir() -> Path:
    env_root = os.environ.get("FI_WEB_ROOT", "")
    if env_root and Path(env_root).exists():
        return Path(env_root)
    # Walk up from server.py to find the web folder
    base = Path(__file__).resolve()
    for _ in range(5):
        base = base.parent
        for candidate in ["web/dist", "web"]:
            p = base / candidate
            if p.exists() and (p / "index.html").exists():
                return p
    return Path()

_static_dir = _find_static_dir()
if _static_dir.exists():
    app.mount("/", StaticFiles(directory=str(_static_dir), html=True), name="static")


# ─────────────────────────── HELPERS ────────────────────────────────────────

def _require_connected():
    if not state.connected or not state.rig:
        raise HTTPException(503, "Not connected to rig")


# ─────────────────────────── ENTRY POINT ────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="FI Rig API server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()
    uvicorn.run(
        "fi_host.server:app",
        host    = args.host,
        port    = args.port,
        reload  = args.reload,
        log_level = "info",
    )


if __name__ == "__main__":
    main()
