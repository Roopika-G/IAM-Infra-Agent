"""Keep incident status in step with what a human did to the agent's pull request.

The agent opens a PR and the incident waits (WAITING_FOR_PR_REVIEW). When a
human merges it the incident becomes MERGED (deploy and verify still to come);
if they close it unmerged it becomes PR_REJECTED. Nothing here merges anything.
"""

import subprocess

import psycopg

from agent.store import DB_DSN, set_status

STATUS_FOR_PR_STATE = {"MERGED": "MERGED", "CLOSED": "PR_REJECTED"}


def pr_state(url: str) -> str | None:
    """OPEN, MERGED or CLOSED as GitHub reports it, or None if gh could not say."""
    try:
        out = subprocess.run(["gh", "pr", "view", url, "--json", "state", "-q", ".state"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def sync_pr_status(get_state=pr_state) -> list[tuple[int, str]]:
    """Update every incident waiting on a PR. Returns [(incident_id, new_status)]."""
    with psycopg.connect(DB_DSN) as conn:
        waiting = conn.execute(
            """SELECT i.id, lr.outcome->>'url' FROM agent.incidents i
               JOIN LATERAL (SELECT outcome FROM agent.runs r WHERE r.incident_id = i.id ORDER BY started_at DESC LIMIT 1) lr ON true
               WHERE i.status = 'WAITING_FOR_PR_REVIEW'""").fetchall()
    changed = []
    for incident_id, url in waiting:
        new = STATUS_FOR_PR_STATE.get(get_state(url) or "") if url else None
        if new:
            set_status(incident_id, new)
            changed.append((incident_id, new))
    return changed
