"""Every data shape the agent passes around, as Pydantic models.

Read top to bottom: what triggered it (Incident), what it saw (Snapshot,
Evidence), what it decided (RepoFix / Escalation), what the policy said
(PolicyResult), what happened (Outcome), and the graph state that holds them.
"""

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


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
