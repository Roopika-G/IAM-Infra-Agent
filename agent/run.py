"""Run the agent on one incident.

    uv run python -m agent.run --incident 7

Needs: ANTHROPIC_API_KEY, AGENT_DB_DSN, PF_ADMIN_PASSWORD (see README), and a
running cluster. Opens a real branch and pull request (or a GitHub issue) on
the repo, so run it deliberately.
"""

import argparse
import asyncio

from agent import gitwork, store
from agent.attempts import retry_notes
from agent.graph import IamAgent
from agent.llm import make_llm
from agent.runlog import RunLog
from agent.schemas import AgentState
from agent.settings import Settings
from agent.tracing import run_span, setup_tracing
from agent.toolbox import Toolbox

STATUS_FOR = {"pr_opened": "WAITING_FOR_PR_REVIEW", "escalated": "ESCALATED"}


async def run_incident(incident_id: int, settings: Settings | None = None) -> AgentState:
    settings = settings or Settings.from_env()
    incident = store.load_incident(incident_id)
    store.set_status(incident.id, "DIAGNOSING")
    log = RunLog(incident.id, settings.model)
    try:
        gitwork.prepare_attempt(incident.id)                  # a fresh branch from what is on GitHub now
        with run_span(incident.id):                       # one Phoenix trace per run
            async with Toolbox(settings) as tools:
                graph = IamAgent(tools, make_llm(settings), settings, log).build()
                final = await graph.ainvoke(AgentState(incident=incident, retry_notes=retry_notes(incident.id)), config={"recursion_limit": 80})
    except Exception as e:
        log.event("error", {"error": repr(e)})
        log.finish("ERROR", error=repr(e))
        store.set_status(incident.id, "NEW")     # nothing happened; let it be tried again
        raise
    state = AgentState(**final)
    log.finish(state.outcome.kind, state.outcome.model_dump())
    store.set_status(incident.id, STATUS_FOR[state.outcome.kind])
    return state


def main() -> None:
    setup_tracing()
    parser = argparse.ArgumentParser(description="Run the agent on one incident.")
    parser.add_argument("--incident", type=int, required=True)
    state = asyncio.run(run_incident(parser.parse_args().incident))
    print(f"decision: {state.decision.kind}  ({state.decision.diagnosis.confidence.value} confidence)")
    print(f"root cause: {state.decision.diagnosis.root_cause}")
    print(f"policy: {'passed' if state.policy.passed else 'refused check ' + str(state.policy.failed_check)}")
    print(f"outcome: {state.outcome.kind}  {state.outcome.url or ''}  {state.outcome.detail}")


if __name__ == "__main__":
    main()
