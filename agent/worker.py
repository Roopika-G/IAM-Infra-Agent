"""The trigger: watch for Sim A incidents and run the agent on each one.

    uv run python -m agent.worker

Run it next to agent/detector.py. The detector writes incidents; this picks
up each new Sim A incident exactly once (it claims it by setting the status
to DIAGNOSING) and runs the agent. Only incidents seen in the last 15 minutes
are eligible, so a freshly started worker never acts on old incidents. Same
environment as agent.run.
"""

import asyncio
import os
import time
import traceback

from agent import store
from agent.deploy import run_deploy
from agent.pr_sync import sync_pr_status
from agent.run import run_incident
from agent.settings import Settings
from agent.tracing import setup_tracing
from agent.trigger import is_sim_a

POLL_SECONDS = 10
PR_SYNC_SECONDS = 15      # how often to ask GitHub whether a PR was merged


def main() -> None:
    settings = Settings.from_env()
    if setup_tracing():
        print("worker: sending traces to Phoenix")
    print(f"worker: watching for Sim A incidents every {POLL_SECONDS}s (Ctrl+C to stop)")
    can_deploy = bool(os.environ.get("HELM_DEPLOY_TOKEN"))
    if not can_deploy:
        print("worker: HELM_DEPLOY_TOKEN is not set, so merged pull requests will NOT be deployed")
    last_sync = 0.0
    while True:
        if time.time() - last_sync > PR_SYNC_SECONDS:           # has a human merged or closed a PR?
            last_sync = time.time()
            for incident_id, status in sync_pr_status():
                print(f"worker: incident #{incident_id} -> {status}")
        # A merged PR is the human approval: the worker deploys it (plain code, no LLM; see agent/deploy.py).
        merged = store.claim_merged() if can_deploy else None
        if merged:
            print(f"worker: deploying the merged fix for incident #{merged}")
            try:
                result = asyncio.run(run_deploy(merged, settings))
                print(f"worker: incident #{merged} deploy -> {result.status}: {result.detail}")
            except Exception:
                traceback.print_exc()
            continue
        incident = store.claim_new(is_sim_a)
        if incident:
            print(f"worker: incident #{incident.id} claimed: {incident.sample_message[:80]}")
            try:
                state = asyncio.run(run_incident(incident.id, settings))
                print(f"worker: incident #{incident.id} -> {state.outcome.kind} {state.outcome.url or ''}")
            except Exception:
                traceback.print_exc()
        else:
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
