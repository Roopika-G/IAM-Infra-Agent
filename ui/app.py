"""Self-healing IAM agent UI backend: a small FastAPI JSON API over ui/data.py.
The React frontend lives in ui/web (Vite + TypeScript).

Dev (two terminals, repo root):
    export AGENT_DB_DSN=... PF_ADMIN_PASSWORD=...        # see README
    uv run uvicorn ui.app:app --port 8000
    cd ui/web && npm run dev                              # http://localhost:5173, proxies /api

Single-process (after `cd ui/web && npm run build`): uvicorn also serves
ui/web/dist at "/".

Localhost use only: there is no authentication, and the Simulations endpoints
can redeploy the cluster.
"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ui import data

app = FastAPI(title="Self-Healing IAM Agent")


@app.get("/api/fleet")
def fleet():
    return data.fleet()


@app.get("/api/incidents")
def incidents(scope: str = "open"):
    return data.list_incidents(only_open=(scope != "all"))


@app.get("/api/incidents/{incident_id}")
def incident(incident_id: int):
    found = data.get_incident(incident_id)
    if not found:
        raise HTTPException(404, "No such incident")
    return found


@app.get("/api/approvals")
def approvals():
    return data.remediation_prs()


@app.get("/api/simulations/a")
def sim_a_status():
    return data.sim_a_status()


@app.post("/api/simulations/a/{mode}")
def sim_a_run(mode: str):
    error = data.sim_a_run(mode)
    if error:
        raise HTTPException(409, error)
    return data.sim_a_status()


DIST = Path(__file__).parent / "web" / "dist"
if DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        """Serve built files as-is, and index.html for any client-side route
        (e.g. /incidents/7) so a direct load or refresh works."""
        candidate = (DIST / path).resolve()
        if path and candidate.is_file() and DIST.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(DIST / "index.html")
