"""The agent graph (LangGraph). Read this file top to bottom.

    intake -> agent <-> tools            the LLM investigates with read tools
                 |
                 v  (it calls a decision tool, or the step limit forces one)
              decision -> policy -> apply_fix -> END       (a PR is opened)
                             |          |
                             +--------> escalate -> END     (a GitHub issue is opened)

Nodes that think (LLM): agent.   Nodes that act (plain code): everything else.
The LLM only ever sees read tools. Writing happens in apply_fix and escalate,
and only after policy has passed the decision.
"""

import time
from datetime import datetime, timezone

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from agent import prompt_loader
from agent.decisions import DECISION_NAMES, DECISION_TOOLS, parse_decision
from agent.intake import build_snapshot, format_evidence
from agent.policy import run_policy
from agent.runlog import NullRunLog, message_record
from agent.schemas import AgentState, Diagnosis, Escalation, Evidence, Outcome, RepoFix
from agent.settings import Settings
from agent.toolbox import ToolFailed

EXTRA_TURNS = 5   # LLM turns allowed beyond the tool budget before we give up


class IamAgent:
    def __init__(self, tools, llm, settings: Settings, log=None):
        self.tools = tools                  # Toolbox, or anything with call() and read_tools()
        self.settings = settings
        self.log = log or NullRunLog()      # records every step (agent/runlog.py)
        self.tools.on_call = self.log.tool_call
        self.read_tools = {t.name: t for t in tools.read_tools()}
        self.llm = llm.bind_tools(list(self.read_tools.values()) + DECISION_TOOLS)
        self.llm_must_decide = llm.bind_tools(DECISION_TOOLS, tool_choice="any")

    # ------------------------------------------------------------- nodes

    async def intake(self, state: AgentState) -> dict:
        snapshot, evidence = await build_snapshot(self.tools, state.incident, self.settings.evidence_chars)
        first = prompt_loader.render(
            "trigger_message",
            incident_id=state.incident.id, trigger_type="log", trigger_payload=state.incident.sample_message,
            snapshot_time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            snapshot_summary=snapshot.summary(), evidence_list=format_evidence(evidence),
        )
        system = prompt_loader.render("system", max_steps=self.settings.max_steps)
        self.log.event("snapshot", snapshot.model_dump())
        for e in evidence:
            self.log.event("evidence", e.model_dump())
        self.log.event("prompt", {"system": system, "first_message": first, "retry_notes": state.retry_notes})
        notes = [HumanMessage(n) for n in state.retry_notes]          # empty on a first attempt
        return {"snapshot": snapshot, "evidence": evidence, "messages": [SystemMessage(system), HumanMessage(first), *notes]}

    async def agent(self, state: AgentState) -> dict:
        """One LLM turn. Once the tool budget is spent it may only decide."""
        out_of_budget = state.steps_used >= self.settings.max_steps
        added = []
        if out_of_budget and not any(isinstance(m, HumanMessage) and "may no longer call read tools" in str(m.content) for m in state.messages):
            added.append(HumanMessage(prompt_loader.render("forced_decision", max_steps=self.settings.max_steps)))
        llm = self.llm_must_decide if out_of_budget else self.llm
        shown = list(state.messages[self.log.logged_messages:]) + added      # what the model has not been shown before
        started = time.monotonic()
        reply = await llm.ainvoke(list(state.messages) + added)
        self.log.event("llm_call", {
            "turn": state.turns + 1, "forced_to_decide": out_of_budget, "model": self.settings.model,
            "new_input": [message_record(m) for m in shown], "output": message_record(reply),
            "usage": getattr(reply, "usage_metadata", None), "stop_reason": (getattr(reply, "response_metadata", None) or {}).get("stop_reason"),
        }, duration_ms=int((time.monotonic() - started) * 1000))
        self.log.logged_messages = len(state.messages) + len(added) + 1
        added.append(reply)
        if not reply.tool_calls:
            added.append(HumanMessage("Continue with a tool call, or finish by calling a decision tool."))
        return {"messages": added, "turns": state.turns + 1}

    async def run_tools(self, state: AgentState) -> dict:
        """Run the read tools the LLM asked for and record each result as numbered evidence."""
        evidence = list(state.evidence)
        replies = []
        for call in _last_ai(state).tool_calls:
            tool = self.read_tools.get(call["name"])
            text = await tool.ainvoke(call["args"]) if tool else f"ERROR: {call['name']} is not an available tool"
            eid = f"e{len(evidence) + 1}"
            evidence.append(Evidence(id=eid, tool=call["name"], args=call["args"], summary=str(text)[: self.settings.evidence_chars]))
            self.log.event("evidence", evidence[-1].model_dump())
            replies.append(ToolMessage(content=f"[{eid}] {text}", tool_call_id=call["id"], name=call["name"]))
        return {"messages": replies, "evidence": evidence, "steps_used": state.steps_used + len(replies)}

    async def record_decision(self, state: AgentState) -> dict:
        """Turn the LLM's decision tool call into a Decision, or tell it what was wrong."""
        calls = _last_ai(state).tool_calls
        decision_call = next(c for c in calls if c["name"] in DECISION_NAMES)
        try:
            decision = parse_decision(decision_call["name"], decision_call["args"])
            self.log.event("decision", decision.model_dump(mode="json"))
            return {"decision": decision}
        except ValidationError as e:
            problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
            return {"messages": [
                ToolMessage(content=f"ERROR: invalid arguments: {problems}. Call the decision tool again with valid arguments."
                            if c is decision_call else "not run: the decision call was invalid", tool_call_id=c["id"])
                for c in calls]}

    async def give_up(self, state: AgentState) -> dict:
        """The LLM never reached a decision. Escalate rather than loop forever."""
        decision = Escalation(
            checked=[e.tool for e in state.evidence], unknown=["why the agent did not reach a decision"],
            next_step="Investigate this incident by hand.",
            diagnosis=Diagnosis(root_cause="unknown", confidence="low", evidence_ids=[e.id for e in state.evidence][:3],
                                explanation="The agent used all its turns without deciding."))
        return {"decision": decision}

    async def policy(self, state: AgentState) -> dict:
        decision = state.decision
        golden = {}
        if isinstance(decision, RepoFix):
            for change in decision.changes:
                try:
                    golden[change.key_path] = (await self.tools.call("get_baseline_value", {"key": change.key_path}))["golden_value"]
                except ToolFailed:
                    golden[change.key_path] = None
        result = run_policy(decision, {e.id for e in state.evidence}, state.incident, golden)
        update: dict = {"policy": result}
        self.log.event("policy", result.model_dump(mode="json"))
        if not result.passed:
            update["decision"] = _refused(decision, f"Policy refused check {result.failed_check} ({result.reason}).")
        return update

    async def apply_fix(self, state: AgentState) -> dict:
        """Patch values.yaml on a branch, check it renders, push, open the PR."""
        fix: RepoFix = state.decision
        iid = state.incident.id
        try:
            sha = (await self.tools.call("read_values_yaml_key", {"key_path": fix.changes[0].key_path}))["file_sha256"]
            for change in fix.changes:
                patched = await self.tools.call("patch_helm_values", {
                    "key_path": change.key_path, "new_value": change.new_value, "expected_file_sha256": sha, "incident_id": iid})
                sha = patched["new_file_sha256"]
            render = await self.tools.call("helm_render_validate", {"incident_id": iid})
            if not render["valid"]:
                return {"decision": _refused(fix, f"The Helm render check failed: {render['errors']}")}
            await self.tools.call("push_remediation_branch", {"incident_id": iid})
            body = prompt_loader.render(
                "pr_body", root_cause=fix.diagnosis.root_cause, confidence=fix.diagnosis.confidence.value,
                evidence="\n".join(f"- {e.id} {e.tool}: {e.summary}" for e in state.evidence if e.id in fix.diagnosis.evidence_ids),
                diff=patched["unified_diff"], render_check=f"valid. {render['patch_changed_lines']} rendered line(s) change.",
                error=state.incident.sample_message)
            pr = await self.tools.call("create_pull_request", {
                "incident_id": iid, "title": f"Fix incident #{iid}: {fix.diagnosis.root_cause[:90]}", "body": body})
            await self.tools.call("add_pr_comment", {"pr_number": pr["number"], "body": "Helm render diff for this change:\n\n```diff\n" + render["rendered_diff"] + "\n```"})
        except ToolFailed as e:
            return {"decision": _refused(fix, f"Applying the fix failed: {e}")}
        outcome = Outcome(kind="pr_opened", url=pr["url"], detail=f"PR #{pr['number']} opened for a human to review")
        self.log.event("outcome", outcome.model_dump())
        return {"outcome": outcome}

    async def escalate(self, state: AgentState) -> dict:
        d: Escalation = state.decision
        report = prompt_loader.render(
            "escalation_report", id=state.incident.id, title=d.diagnosis.root_cause[:80], trigger=state.incident.sample_message,
            checked="\n".join(f"- {c}" for c in d.checked) or "- (nothing recorded)",
            found=d.diagnosis.root_cause, confidence=d.diagnosis.confidence.value,
            ruled_out="\n".join(f"- still unknown: {u}" for u in d.unknown) or "- (nothing recorded)",
            next_step=d.next_step)
        try:
            issue = await self.tools.call("create_github_issue", {
                "title": d.diagnosis.root_cause[:100], "body": report, "incident_id": state.incident.id})
            outcome = Outcome(kind="escalated", url=issue["url"], detail="GitHub issue opened for a human")
        except ToolFailed as e:
            outcome = Outcome(kind="escalated", url=None, detail=f"Escalated, but the GitHub issue could not be created: {e}")
        self.log.event("outcome", outcome.model_dump())
        return {"outcome": outcome}

    def _recorded(self, name: str):
        """Wrap a node so the log shows when it started, how long it took, and what it changed."""
        node = getattr(self, name)

        async def run(state: AgentState) -> dict:
            self.log.node = name
            self.log.event("node_start", {"steps_used": state.steps_used, "evidence": len(state.evidence)}, node=name)
            started = time.monotonic()
            try:
                update = await node(state)
            except Exception as e:
                self.log.event("error", {"error": repr(e)}, node=name)
                raise
            self.log.event("node_end", {"changed": sorted(update)}, node=name, duration_ms=int((time.monotonic() - started) * 1000))
            return update

        return run

    # ----------------------------------------------------------- routing

    def after_agent(self, state: AgentState) -> str:
        calls = _last_ai(state).tool_calls
        if any(c["name"] in DECISION_NAMES for c in calls):
            return "decision"
        if calls:
            return "tools"
        return "give_up" if state.turns >= self.settings.max_steps + EXTRA_TURNS else "agent"

    @staticmethod
    def after_decision(state: AgentState) -> str:
        return "agent" if state.decision is None else "policy"

    @staticmethod
    def after_policy(state: AgentState) -> str:
        return "apply_fix" if state.decision.kind == "repo_fix" else "escalate"

    @staticmethod
    def after_apply(state: AgentState) -> str:
        return END if state.outcome else "escalate"      # apply_fix swaps in an escalation when it fails

    # ------------------------------------------------------------- graph

    def build(self):
        g = StateGraph(AgentState)
        for name in ("intake", "agent", "run_tools", "record_decision", "give_up", "policy", "apply_fix", "escalate"):
            g.add_node(name, self._recorded(name))
        g.add_edge(START, "intake")
        g.add_edge("intake", "agent")
        g.add_conditional_edges("agent", self.after_agent, {"tools": "run_tools", "decision": "record_decision", "give_up": "give_up", "agent": "agent"})
        g.add_edge("run_tools", "agent")
        g.add_conditional_edges("record_decision", self.after_decision, {"agent": "agent", "policy": "policy"})
        g.add_edge("give_up", "policy")
        g.add_conditional_edges("policy", self.after_policy, {"apply_fix": "apply_fix", "escalate": "escalate"})
        g.add_conditional_edges("apply_fix", self.after_apply, {END: END, "escalate": "escalate"})
        g.add_edge("escalate", END)
        return g.compile()


def _last_ai(state: AgentState) -> AIMessage:
    return next(m for m in reversed(state.messages) if isinstance(m, AIMessage))


def _refused(decision, why: str) -> Escalation:
    """Replace a decision that cannot run with an escalation that says why."""
    d = decision.diagnosis
    return Escalation(
        checked=getattr(decision, "checked", []) or [f"proposed: {getattr(decision, 'kind', 'decision')}"],
        unknown=getattr(decision, "unknown", []),
        next_step=f"{why} A human should review what the agent found: {d.explanation}",
        diagnosis=Diagnosis(root_cause=d.root_cause, confidence=d.confidence, evidence_ids=d.evidence_ids, explanation=f"{why} {d.explanation}"))
