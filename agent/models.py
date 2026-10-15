"""Everything the agent is made of that is not logic: the data shapes, the settings,
the decision tools the LLM ends with, the prompt files, and the chat model.
"""

import os
import re
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AnyMessage
from langchain_core.tools import StructuredTool
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


# ---- settings --------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]

# Environment an MCP server needs. A stdio MCP server does NOT inherit your
# shell, so these are passed to each server explicitly.
MCP_ENV_KEYS = ("HOME", "PATH", "AGENT_DB_DSN", "PF_ADMIN_PASSWORD", "PF_ADMIN_USER", "PF_ADMIN_URL", "KUBE_CONTEXT", "KUBECONFIG", "HELM_DEPLOY_TOKEN")


class Settings(BaseModel):
    model: str = "claude-haiku-5-5"
    max_steps: int = Field(8, description="Most read-tool calls the agent may make before it must decide.")
    evidence_chars: int = 400
    tool_result_chars: int = 6000
    repo_root: Path = REPO_ROOT
    mcp_env: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "Settings":
        """Read .env (if present) without overriding the shell, then the environment."""
        load_dotenv(env_file or REPO_ROOT / ".env")
        env = {k: os.environ[k] for k in MCP_ENV_KEYS if k in os.environ}
        env.setdefault("HF_HUB_OFFLINE", "1")  # the embedding model is already cached locally
        return cls(model=os.environ.get("AGENT_MODEL", "claude-haiku-5-5"), mcp_env=env)


# ---- data shapes -----------------------------------------------------------

class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


# --- what triggered it ------------------------------------------------------

class Incident(BaseModel):
    """One row of agent.incidents: a de-duplicated fault the detector found."""
    id: int
    status: str
    pf_role: str | None = None
    logger: str | None = None
    sample_message: str
    first_seen: datetime
    last_seen: datetime
    occurrence_count: int


# --- what it saw ------------------------------------------------------------

class Evidence(BaseModel):
    """One tool result, numbered so a decision can cite it (e1, e2, ...)."""
    id: str
    tool: str
    args: dict = Field(default_factory=dict)
    summary: str


class DriftRow(BaseModel):
    key: str
    golden: str
    live: str | None
    state: Literal["ok", "drift", "unknown"]


class PodRow(BaseModel):
    name: str
    phase: str
    ready: str
    restarts: int


class Snapshot(BaseModel):
    """The picture of the system taken at intake, before the LLM starts."""
    pods: list[PodRow] = Field(default_factory=list)
    drift: list[DriftRow] = Field(default_factory=list)
    license_days: int | None = None
    cert_days: int | None = None
    replication: str | None = None
    recent_errors: int | None = None
    unavailable: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        lines = []
        if self.pods:
            lines.append("pods: " + " · ".join(f"{p.name.split('-')[-3] if p.name.count('-') > 2 else p.name} {p.ready} {p.phase} (restarts {p.restarts})" for p in self.pods))
        drifted = [d for d in self.drift if d.state == "drift"]
        if self.drift:
            lines.append(
                f"drift scan: {len(drifted)} of {len(self.drift)} baseline keys differ from the golden value"
                + "".join(f"\n  - {d.key}: golden={d.golden!r} live={d.live!r}" for d in drifted)
            )
        if self.license_days is not None:
            lines.append(f"license: {self.license_days} days until expiry")
        if self.cert_days is not None:
            lines.append(f"soonest certificate expiry: {self.cert_days} days")
        if self.replication:
            lines.append(f"PF config replication: {self.replication}")
        if self.recent_errors is not None:
            lines.append(f"recent ERROR log lines: {self.recent_errors}")
        if self.unavailable:
            lines.append("could not read: " + ", ".join(self.unavailable))
        return "\n".join(lines) or "(snapshot empty)"


# --- what it decided --------------------------------------------------------

class Diagnosis(BaseModel):
    root_cause: str
    confidence: Confidence
    evidence_ids: list[str]
    explanation: str


class Change(BaseModel):
    key_path: str = Field(description="Dotted path into values.yaml, e.g. pingfederate-admin.envs.POSTGRES_JDBC_URL")
    new_value: str


class RepoFix(BaseModel):
    kind: Literal["repo_fix"] = "repo_fix"
    file: str
    changes: list[Change] = Field(min_length=1)
    diagnosis: Diagnosis


class Escalation(BaseModel):
    kind: Literal["escalate"] = "escalate"
    checked: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    next_step: str
    diagnosis: Diagnosis


Decision = RepoFix | Escalation


# --- the policy check and what happened -----------------------------------------

class CheckResult(BaseModel):
    number: int
    name: str
    result: Literal["pass", "fail", "na", "skip"]
    note: str


class PolicyResult(BaseModel):
    passed: bool
    kind: str
    failed_check: int | None = None
    reason: str | None = None
    rows: list[CheckResult]


class Outcome(BaseModel):
    kind: Literal["pr_opened", "escalated"]
    url: str | None = None
    detail: str


# --- the graph state ----------------------------------------------------------

class AgentState(BaseModel):
    incident: Incident
    retry_notes: list[str] = Field(default_factory=list)   # what earlier attempts did and how they failed
    snapshot: Snapshot | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    messages: Annotated[list[AnyMessage], add_messages] = Field(default_factory=list)
    steps_used: int = 0          # read-tool calls made so far (the budget)
    turns: int = 0               # times the LLM was called (a hard stop against endless loops)
    decision: RepoFix | Escalation | None = Field(default=None, discriminator="kind")
    policy: PolicyResult | None = None
    outcome: Outcome | None = None


# ---- decision tools (how the LLM finishes) ---------------------------------

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


# ---- prompt and template files ---------------------------------------------

FOLDERS = (Path(__file__).parent / "prompts", Path(__file__).parent / "templates")
_PLACEHOLDER = re.compile(r"{(\w+)}")


def load_prompt(name: str) -> str:
    for folder in FOLDERS:
        if (folder / f"{name}.md").exists():
            return (folder / f"{name}.md").read_text()
    raise FileNotFoundError(f"no prompt or template named {name!r}")


def placeholders(name: str) -> set[str]:
    return set(_PLACEHOLDER.findall(load_prompt(name)))


def render(name: str, **values: object) -> str:
    missing = placeholders(name) - values.keys()
    if missing:
        raise KeyError(f"prompt {name!r} needs values for: {sorted(missing)}")
    return load_prompt(name).format(**values)


# ---- the chat model --------------------------------------------------------

def make_llm(settings: Settings) -> ChatAnthropic:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set. The agent needs an LLM key to run.")
    # No temperature: current Claude models reject non-default values.
    return ChatAnthropic(model=settings.model, max_tokens=4096)
