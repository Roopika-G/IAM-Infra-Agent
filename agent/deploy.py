"""After a human merges the agent's pull request: deploy that exact commit and check it worked.

NOTE: for now the worker does this by itself, in plain code. The LLM is NOT given
the deploy tool: deploying changes the live cluster, and this project keeps the
model away from every write. The merge is the human approval. The LLM is meant
to come back in only when verification FAILS: a retry would hand it this
attempt and what went wrong, so it can propose a different fix. That retry
loop is not built yet; for now a failed deploy is rolled back and escalated.

Steps: fetch the merged commit -> helm upgrade (atomic: a failed rollout undoes
itself) -> wait for the pods -> check it worked (pods ready, no config drift, the
error has not come back) -> RESOLVED, and the incident is closed for good.
If the check fails: roll back to the revision that was running before, and set
RETRY so the agent tries again with this attempt in its context. After
MAX_ATTEMPTS the incident goes to FAILED and a GitHub issue asks a human.
While an incident is open (WAITING_FOR_PR_REVIEW, MERGED, DEPLOYING, RETRY) the
same error occurring again just adds to it; it never starts a second agent.
"""

import asyncio
import os
import subprocess
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from agent.models import Settings
from agent.records import MAX_ATTEMPTS, RunLog, attempt_count, latest_run, run_span, set_status
from agent.tools import SERVER_SCRIPTS, ToolFailed, Toolbox, component_of, scan_drift

# helm_deploy is deliberately NOT in toolbox.SERVER_SCRIPTS (what the agent's LLM run uses): only this module starts it.
DEPLOY_SERVERS = {"helm_deploy": "mcp_servers/helm_deploy/server.py",
                  **{name: SERVER_SCRIPTS[name] for name in ("knowledge", "kubernetes", "github_pr", "helm_ops")}}
UPGRADE_TIMEOUT = 20 * 60.0      # seconds: a rollout with --wait can take a while
PODS_READY_WAIT_SECONDS = 300
OBSERVE_SECONDS = 60       # how long the error must stay away before we call it fixed
POLL_SECONDS = 10


class DeployResult(BaseModel):
    status: Literal["resolved", "rolled_back", "failed"]
    detail: str
    revision: int | None = None


def pods_ready(pods: list[dict]) -> bool:
    mine = [p for p in pods if component_of(p["pod"])]
    return bool(mine) and all(p["phase"] == "Running" and p["ready"].split("/")[0] == p["ready"].split("/")[1] != "0" for p in mine)


async def verify(tools, since_iso: str, sleep=asyncio.sleep, wait=PODS_READY_WAIT_SECONDS, observe=OBSERVE_SECONDS, poll=POLL_SECONDS) -> tuple[bool, str]:
    """Did the deploy fix it? Returns (ok, why)."""
    waited = 0
    while not pods_ready(await tools.call("get_pod_status", {})):
        if waited >= wait:
            return False, f"the pods were not all ready within {wait}s"
        await sleep(poll)
        waited += poll
    await sleep(observe)
    _, drift = await scan_drift(tools, [])
    drifted = [d for d in drift if d.state == "drift"]
    if drifted:
        return False, f"{len(drifted)} key(s) still differ from the golden value: " + "; ".join(f"{d.key} is {d.live!r}" for d in drifted)
    again = await tools.call("get_logs", {"level": "ERROR", "contains": "data source instance", "since": since_iso, "limit": 5})
    if again:
        return False, "the error 'Unable to load custom data source instance' appeared again after the deploy"
    return True, f"all pods ready, 0 of {len(drift)} keys drifted, and the error did not return for {observe}s"


async def deploy_and_verify(tools, commit: str, token: str, log, **timing) -> DeployResult:
    log.event("deploy_start", {"commit": commit}, node="deploy")
    previous = await deployed_revision(tools)       # what to roll back to: the revision running BEFORE this upgrade
    try:
        up = await tools.call("helm_upgrade_release", {"commit_sha": commit, "token": token}, timeout=UPGRADE_TIMEOUT)
    except ToolFailed as e:
        return DeployResult(status="failed", detail=f"helm upgrade failed (helm undoes a failed rollout by itself): {e}")
    since = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    ok, why = await verify(tools, since, **timing)
    log.event("verify", {"ok": ok, "detail": why}, node="deploy")
    if ok:
        return DeployResult(status="resolved", detail=why, revision=up["revision"])
    if previous is None:
        return DeployResult(status="failed", detail=f"{why}. There was no earlier deployed revision to roll back to.", revision=up["revision"])
    try:
        await tools.call("helm_rollback_release", {"revision": previous, "token": token}, timeout=UPGRADE_TIMEOUT)
    except ToolFailed as e:
        return DeployResult(status="failed", detail=f"{why}. Rolling back to revision {previous} also failed: {e}", revision=up["revision"])
    return DeployResult(status="rolled_back", detail=f"{why}. Rolled back to revision {previous}.", revision=up["revision"])


async def deployed_revision(tools) -> int | None:
    """The Helm revision that is currently deployed (failed or half-finished revisions do not count)."""
    try:
        history = (await tools.call("get_helm_release", {}))["history"]
    except (ToolFailed, KeyError):
        return None
    deployed = [h["revision"] for h in history if h.get("status") == "deployed"]
    return max(deployed) if deployed else None


def next_status(result: DeployResult, attempts: int) -> str:
    """RESOLVED closes the incident for good. A failed deploy lets the agent try again, up to MAX_ATTEMPTS, then a human is asked."""
    if result.status == "resolved":
        return "RESOLVED"
    return "RETRY" if attempts < MAX_ATTEMPTS else "FAILED"


def merge_commit(pr_url: str) -> str:
    out = subprocess.run(["gh", "pr", "view", pr_url, "--json", "mergeCommit", "-q", ".mergeCommit.oid"], capture_output=True, text=True, timeout=30)
    commit = out.stdout.strip()
    if out.returncode != 0 or not commit:
        raise RuntimeError(f"could not find the merge commit of {pr_url}: {out.stderr.strip()[:200]}")
    return commit


def fetch_main(root) -> None:
    out = subprocess.run(["git", "fetch", "github", "main"], cwd=root, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"git fetch failed: {out.stderr.strip()[:200]}")


async def run_deploy(incident_id: int, settings: Settings | None = None) -> DeployResult:
    """Deploy the merged fix for one incident. The incident must already be claimed (DEPLOYING)."""
    settings = settings or Settings.from_env()
    token = os.environ["HELM_DEPLOY_TOKEN"]
    run_id, outcome = latest_run(incident_id)
    log = RunLog.resume(run_id)
    try:
        commit = merge_commit(outcome["url"])
        fetch_main(settings.repo_root)
        with run_span(incident_id, "deploy"):
            async with Toolbox(settings, servers=DEPLOY_SERVERS) as tools:
                tools.on_call = log.tool_call
                result = await deploy_and_verify(tools, commit, token, log)
                final_failure = next_status(result, attempt_count(incident_id)) == "FAILED"
                if final_failure:                       # out of attempts: ask a human (otherwise the agent tries again)
                    try:
                        await tools.call("create_github_issue", {
                            "title": f"Deploy of the fix for incident #{incident_id} did not work: {result.status}",
                            "body": f"## What happened\nThe agent's pull request was merged and deployed (commit {commit}), but the check failed.\n\n## Result\n{result.detail}\n\n## What a human must do\nLook at the cluster and the pull request; the agent has not retried.",
                            "incident_id": incident_id})
                    except ToolFailed as e:
                        result.detail += f" (the GitHub issue could not be opened: {e})"
    except Exception as e:                  # never leave an incident stuck in DEPLOYING
        result = DeployResult(status="failed", detail=f"the deploy step crashed: {e!r}")
        log.event("error", {"error": repr(e)}, node="deploy")
    status = next_status(result, attempt_count(incident_id))
    log.event("deploy_result", {**result.model_dump(), "next_status": status}, node="deploy")
    set_status(incident_id, status)
    return result
