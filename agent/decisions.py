"""The decision tools: how the LLM ends its investigation.

Calling one of these does nothing by itself. The graph reads the call,
turns it into a Decision, and the policy check and the code nodes decide
what actually runs. propose_repo_fix and escalate are all Sim A needs;
request_runtime_action arrives with the first runtime-action issue.
"""

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from agent.schemas import Change, Confidence, Diagnosis, Escalation, RepoFix


class ProposeRepoFix(BaseModel):
    """The fix is a change to values.yaml. Give each key's exact dotted path and its new value."""
    file: str = Field(description="Always helm/ping-devops/values.yaml")
    changes: list[Change] = Field(min_length=1, description="Every key to change, with its new value")
    root_cause: str
    confidence: Confidence
    evidence_ids: list[str] = Field(description="Evidence ids (e1, e2, ...) that support the root cause")
    explanation: str = Field(description="Short, for a human reviewer who has not seen your work")


class EscalateArgs(BaseModel):
    """Hand this to a human: it must not or cannot be fixed automatically, or the cause is unclear."""
    root_cause: str = Field(description="The cause, or 'unknown'")
    confidence: Confidence
    evidence_ids: list[str]
    checked: list[str] = Field(description="What you checked, one item per check")
    unknown: list[str] = Field(description="What is still unknown")
    next_step: str = Field(description="What a human should do")
    explanation: str


def _record(**kwargs) -> str:
    return "decision recorded"


DECISION_TOOLS = [
    StructuredTool.from_function(func=_record, name="propose_repo_fix", description=ProposeRepoFix.__doc__, args_schema=ProposeRepoFix),
    StructuredTool.from_function(func=_record, name="escalate", description=EscalateArgs.__doc__, args_schema=EscalateArgs),
]
DECISION_NAMES = {t.name for t in DECISION_TOOLS}


def parse_decision(name: str, args: dict) -> RepoFix | Escalation:
    """Tool call -> Decision. Raises pydantic.ValidationError on bad arguments, ValueError on an unknown tool."""
    if name == "propose_repo_fix":
        a = ProposeRepoFix(**args)
        return RepoFix(file=a.file, changes=a.changes,
                       diagnosis=Diagnosis(root_cause=a.root_cause, confidence=a.confidence, evidence_ids=a.evidence_ids, explanation=a.explanation))
    if name == "escalate":
        a = EscalateArgs(**args)
        return Escalation(checked=a.checked, unknown=a.unknown, next_step=a.next_step,
                          diagnosis=Diagnosis(root_cause=a.root_cause, confidence=a.confidence, evidence_ids=a.evidence_ids, explanation=a.explanation))
    raise ValueError(f"unknown decision tool: {name}")
