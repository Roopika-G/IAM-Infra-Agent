"""The agent's control loop.

    uv run python -m agent.worker                 # watch for incidents and act (run beside agent/detector.py)
    uv run python -m agent.worker --incident 7    # run the agent once on one incident

What it does, per incident status:
  NEW / RETRY            start the agent (Sim A errors only; NEW only if seen in the last 15 minutes)
  WAITING_FOR_PR_REVIEW  wait; a human merging or closing the PR is noticed every 15 seconds
  MERGED                 deploy that exact merge commit and check it (agent/deploy.py)
  RESOLVED               closed for good; never touched again

Needs ANTHROPIC_API_KEY, AGENT_DB_DSN and PF_ADMIN_PASSWORD (all in .env) and a running cluster.
The agent opens a real branch and pull request (or a GitHub issue) on this repo.
"""

import argparse
import asyncio
import os
import time
import traceback

from agent.deploy import run_deploy
from agent.graph import IamAgent
from agent.models import AgentState, Incident, Settings, make_llm
from agent.records import (RunLog, claim_merged, claim_new, load_incident, retry_notes, run_span, set_status,
                           setup_tracing, sync_pr_status)
from agent.tools import Toolbox
from mcp_servers import common
from agent.signature import is_simulated_fault

POLL_SECONDS = 10
PR_SYNC_SECONDS = 15
STATUS_FOR = {"pr_opened": "WAITING_FOR_PR_REVIEW", "escalated": "ESCALATED"}


def prepare_attempt(incident_id: int, root=common.REPO_ROOT) -> None:
    """Start every attempt from what is on GitHub right now, on a fresh branch (a retry must not build on the merged one)."""
    common.git("fetch", "github", "main", cwd=root)
    path, branch = root / ".worktrees" / f"inc-{incident_id}", f"remediation/inc-{incident_id}"
    if path.exists():
        common.git("worktree", "remove", "--force", str(path), cwd=root)
    common.git("worktree", "prune", cwd=root)
    if branch in common.git("branch", "--list", branch, cwd=root):
        common.git("branch", "-D", branch, cwd=root)


async def run_incident(incident_id: int, settings: Settings | None = None) -> AgentState:
    settings = settings or Settings.from_env()
    incident = load_incident(incident_id)
    set_status(incident.id, "DIAGNOSING")
    log = RunLog(incident.id, settings.model)
    try:
        prepare_attempt(incident.id)
        with run_span(incident.id):                       # one Phoenix trace per run
            async with Toolbox(settings) as tools:
                graph = IamAgent(tools, make_llm(settings), settings, log).build()
                final = await graph.ainvoke(AgentState(incident=incident, retry_notes=retry_notes(incident.id)), config={"recursion_limit": 80})
    except Exception as e:
        log.event("error", {"error": repr(e)})
        log.finish("ERROR", error=repr(e))
        set_status(incident.id, "NEW")                    # nothing happened; let it be tried again
        raise
    state = AgentState(**final)
    log.finish(state.outcome.kind, state.outcome.model_dump())
    set_status(incident.id, STATUS_FOR[state.outcome.kind])
    return state


def watch(settings: Settings) -> None:
    can_deploy = bool(os.environ.get("HELM_DEPLOY_TOKEN"))
    if setup_tracing():
        print("worker: sending traces to Phoenix")
    if not can_deploy:
        print("worker: HELM_DEPLOY_TOKEN is not set, so merged pull requests will NOT be deployed")
    print(f"worker: watching for Sim A incidents every {POLL_SECONDS}s (Ctrl+C to stop)")
    last_sync = 0.0
    while True:
        if time.time() - last_sync > PR_SYNC_SECONDS:     # has a human merged or closed a PR?
            last_sync = time.time()
            for incident_id, status in sync_pr_status():
                print(f"worker: incident #{incident_id} -> {status}")
        merged = claim_merged() if can_deploy else None   # a merged PR is the human approval: deploy it (plain code, no LLM)
        incident = None if merged else claim_new(lambda i: is_simulated_fault(i.logger, i.sample_message))
        try:
            if merged:
                print(f"worker: deploying the merged fix for incident #{merged}")
                result = asyncio.run(run_deploy(merged, settings))
                print(f"worker: incident #{merged} deploy -> {result.status}: {result.detail}")
            elif incident:
                print(f"worker: incident #{incident.id} claimed: {incident.sample_message[:80]}")
                state = asyncio.run(run_incident(incident.id, settings))
                print(f"worker: incident #{incident.id} -> {state.outcome.kind} {state.outcome.url or ''}")
            else:
                time.sleep(POLL_SECONDS)
        except Exception:
            traceback.print_exc()


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch for incidents, or run the agent once on one.")
    parser.add_argument("--incident", type=int, help="run the agent once on this incident and print the result")
    args = parser.parse_args()
    settings = Settings.from_env()
    if not args.incident:
        return watch(settings)
    setup_tracing()
    state = asyncio.run(run_incident(args.incident, settings))
    print(f"decision: {state.decision.kind}  ({state.decision.diagnosis.confidence.value} confidence)")
    print(f"root cause: {state.decision.diagnosis.root_cause}")
    print(f"policy: {'passed' if state.policy.passed else 'refused check ' + str(state.policy.failed_check)}")
    print(f"outcome: {state.outcome.kind}  {state.outcome.url or ''}  {state.outcome.detail}")


if __name__ == "__main__":
    main()
