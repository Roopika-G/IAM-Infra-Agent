"""Turns one incident and the agent's recorded events into a plain vertical story.

Pure functions (no database, no network) so they can be tested directly.
Each step is {time, title, detail, state, url}; state is done, active, pending or bad.
"""

import json


def _short(text, n=140) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _tail(key: str) -> str:
    return key.rsplit(".", 1)[-1] if key else key


def describe_tool(tool: str, args: dict, result, error: str | None) -> tuple[str, str]:
    """A tool call as (what the agent did, what it found), in plain words."""
    args = args or {}
    if error:
        return f"Tried {tool}", f"it failed: {_short(error)}"
    r = result if isinstance(result, dict) else {}
    if tool == "search_vector":
        hits = result if isinstance(result, list) else []
        return f"Searched the knowledge base for “{_short(args.get('query_text', ''), 60)}”", (f"best match: {hits[0].get('title')}" if hits else "nothing relevant")
    if tool == "get_baseline_value":
        return f"Looked up the known-good value of {_tail(args.get('key', ''))}", _short(r.get("golden_value"))
    if tool == "get_live_config_value":
        return f"Read {args.get('key')} on the running pod", _short(r.get("value"))
    if tool == "read_values_yaml_key":
        return f"Read {_tail(args.get('key_path', ''))} in values.yaml on GitHub", _short(r.get("value"))
    if tool == "get_pf_datastore":
        return "Asked PingFederate how it sees its database connection", _short(r.get("connectionUrl") or r.get("name") or "")
    if tool == "find_in_repo":
        return f"Searched the repo for {_short(args.get('text', ''), 50)}", f"{len((r or {}).get('matches', []))} match(es)"
    if tool == "get_logs":
        return "Read recent PingFederate logs", f"{len(result) if isinstance(result, list) else 0} line(s)"
    if tool == "get_pod_status":
        return "Checked pod status", ", ".join(f"{p['pod'].split('-')[2] if p['pod'].count('-') > 2 else p['pod']} {p['ready']}" for p in (result or [])[:3])
    if tool == "get_helm_release":
        return "Looked at the Helm release history", f"{len(r.get('history', []))} revision(s)"
    return f"Used {tool}", _short(json.dumps(result, default=str), 100)


def build_timeline(incident: dict, runs: list[dict], waiting_for_error: bool = False) -> list[dict]:
    """incident: {status, sample_message, first_seen}. runs: oldest first, each {started_at, status, outcome, model, error, events}."""
    steps: list[dict] = []

    def add(time, title, detail="", state="done", url=None):
        steps.append({"time": time, "title": title, "detail": detail, "state": state, "url": url})

    if waiting_for_error and not incident:
        add(None, "Fault injected, waiting for PingFederate to log the error", "the pods are restarting", "active")
        return steps
    if not incident:
        return steps

    status = incident["status"]
    add(incident["first_seen"], "Error detected", _short(incident["sample_message"], 110))

    pr_url = None
    deployed = None
    for n, run in enumerate(runs, 1):
        if len(runs) > 1:
            add(run["started_at"], f"Attempt {n}", "", "done")
        add(run["started_at"], "Agent started", run.get("model") or "")
        for e in run["events"]:
            d, k = e["data"], e["kind"]
            if k == "snapshot":
                drifted = [x for x in d.get("drift", []) if x.get("state") == "drift"]
                add(e["ts"], "Took a snapshot of the system",
                    f"{len(drifted)} of {len(d.get('drift', []))} config keys differ from their known-good value" + (f" ({_tail(drifted[0]['key'])}...)" if drifted else ""))
            elif k == "tool_call" and e["node"] == "run_tools":
                what, found = describe_tool(d.get("tool"), d.get("args"), d.get("result"), d.get("error"))
                add(e["ts"], what, found)
            elif k == "decision":
                dg = d.get("diagnosis", {})
                add(e["ts"], "Decided: " + ("open a pull request with a fix" if d.get("kind") == "repo_fix" else "hand this to a human"),
                    f"{_short(dg.get('root_cause', ''), 120)} ({dg.get('confidence')} confidence)")
            elif k == "policy":
                add(e["ts"], "Safety check " + ("passed" if d.get("passed") else "blocked the fix"),
                    "" if d.get("passed") else f"check {d.get('failed_check')}: {_short(d.get('reason', ''))}", "done" if d.get("passed") else "bad")
            elif k == "tool_call" and e["node"] == "apply_fix" and not d.get("error"):
                r = d.get("result") if isinstance(d.get("result"), dict) else {}
                if d["tool"] == "patch_helm_values" and r.get("branch"):
                    patches = 1 + (steps[-1]["title"] == "Prepared the fix on a branch" and int(steps[-1]["detail"].split(" key")[0].split()[0]) or 0)
                    if steps[-1]["title"] == "Prepared the fix on a branch":
                        steps.pop()                                   # one line however many keys were changed
                    add(e["ts"], "Prepared the fix on a branch", f"{patches} key{'s' if patches > 1 else ''} changed on {r['branch']} (commit {str(r.get('commit', ''))[:7]})")
                elif d["tool"] == "helm_render_validate":
                    add(e["ts"], "Checked the fix renders", "valid" if r.get("valid") else "not valid", "done" if r.get("valid") else "bad")
                elif d["tool"] == "create_pull_request":
                    pr_url = r.get("url")
                    add(e["ts"], f"Opened pull request #{r.get('number')}", "for a human to review", url=r.get("url"))
            elif k == "outcome" and d.get("kind") == "escalated":
                add(e["ts"], "Opened a GitHub issue for a human", _short(d.get("detail", "")), url=d.get("url"))
            elif k == "deploy_start":
                add(e["ts"], "Merged. Deploying the fix", f"commit {str(d.get('commit', ''))[:7]}")
            elif k == "verify":
                add(e["ts"], "Checked the result", _short(d.get("detail", "")), "done" if d.get("ok") else "bad")
            elif k == "deploy_result":
                deployed = d
                add(e["ts"], {"resolved": "Resolved. The fix is live", "rolled_back": "Check failed. Rolled back", "failed": "Deploy failed"}.get(d.get("status"), "Deploy finished"),
                    "" if d.get("status") == "resolved" else _short(d.get("detail", "")), "done" if d.get("status") == "resolved" else "bad")
            elif k == "error":
                add(e["ts"], "The agent hit an error", _short(d.get("error", "")), "bad")

    # What comes next, from the incident's status
    nxt = {
        "NEW": ("Starting the agent", "active"),
        "DIAGNOSING": ("The agent is working", "active"),
        "WAITING_FOR_PR_REVIEW": ("Waiting for you to merge the pull request", "active"),
        "MERGED": ("Merged. Deploying the fix", "active"),
        "DEPLOYING": ("Deploying and checking the result", "active"),
        "RETRY": ("The deploy check failed. The agent will try again", "active"),
        "ESCALATED": ("Waiting for a human to read the issue", "active"),
        "FAILED": ("Out of attempts. A human has been asked", "bad"),
        "PR_REJECTED": ("The pull request was closed without merging. Waiting for a human", "bad"),
    }.get(status)
    if nxt and not (status == "DEPLOYING" and deployed):
        add(None, nxt[0], "", nxt[1])
    if status in ("WAITING_FOR_PR_REVIEW", "MERGED", "DEPLOYING") and not deployed:
        add(None, "Deploy and check the result", "", "pending")
        add(None, "Resolved", "", "pending")
    return steps
