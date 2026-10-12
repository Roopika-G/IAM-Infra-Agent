"""What earlier attempts on an incident did and how they ended, for a retry.

An attempt is one agent run. If its fix was merged and deployed but the check
failed, the next run is told exactly that, so it does not repeat the same fix.
"""

import psycopg

from agent import prompt_loader
from agent.store import DB_DSN

MAX_ATTEMPTS = 2     # the agent gets this many tries; after that a human is asked


def attempt_count(incident_id: int) -> int:
    with psycopg.connect(DB_DSN) as conn:
        return conn.execute("SELECT count(*) FROM agent.runs WHERE incident_id = %s", (incident_id,)).fetchone()[0]


def render_notes(attempts: list[dict]) -> list[str]:
    """attempts: [{n, changes: [(key, value)], pr_url, deploy_status, deploy_detail}], oldest first."""
    notes = []
    for a in attempts:
        if not a.get("deploy_status"):
            continue                                   # no known result: nothing to learn from
        changed = ", ".join(f"{k} -> {v}" for k, v in a["changes"]) or "no file change"
        notes.append(prompt_loader.render(
            "retry_context", n=a["n"],
            summary=f"changed {changed}" + (f" (pull request {a['pr_url']})" if a.get("pr_url") else ""),
            result=f"{a['deploy_status']}: {a['deploy_detail']}"))
    return notes


def retry_notes(incident_id: int) -> list[str]:
    attempts = []
    with psycopg.connect(DB_DSN) as conn:
        runs = conn.execute("SELECT id, outcome FROM agent.runs WHERE incident_id = %s ORDER BY started_at", (incident_id,)).fetchall()
        for n, (run_id, outcome) in enumerate(runs, 1):
            events = {k: d for k, d in conn.execute(
                "SELECT kind, data FROM agent.run_events WHERE run_id = %s AND kind IN ('decision', 'deploy_result') ORDER BY seq", (run_id,)).fetchall()}
            decision, deploy = events.get("decision") or {}, events.get("deploy_result") or {}
            attempts.append({"n": n, "changes": [(c["key_path"], c["new_value"]) for c in decision.get("changes", [])],
                             "pr_url": (outcome or {}).get("url"), "deploy_status": deploy.get("status"), "deploy_detail": deploy.get("detail")})
    return render_notes(attempts)
