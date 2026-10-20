"""Tests for the agent: policy, intake, prompts, and the whole graph run with a
scripted LLM and fake tools (no API key, no cluster)."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import models  # noqa: E402
from agent.graph import IamAgent  # noqa: E402
from agent.policy import run_policy  # noqa: E402
from agent.records import NullRunLog  # noqa: E402
from agent.models import AgentState, Change, Diagnosis, Incident, RepoFix, Settings  # noqa: E402
from agent.tools import READ_TOOLS, ToolFailed, Toolbox, build_snapshot, parse_result  # noqa: E402

GOLD = "jdbc:postgresql://postgres.pingfederate.svc.cluster.local:5432/postgres"
BAD = "jdbc:postgresql://postgres-wrong-host.pingfederate.svc.cluster.local:5432/postgres"
ADMIN_KEY = "pingfederate-admin.envs.POSTGRES_JDBC_URL"
ENGINE_KEY = "pingfederate-engine.envs.POSTGRES_JDBC_URL"
VALUES = "helm/ping-devops/values.yaml"
NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def incident(status="NEW", message='Unable to load custom data source instance: JDBC-FD67'):
    return Incident(id=7, status=status, pf_role="admin", logger="org.sourceid.saml20.domain.mgmt.impl.DataSourceManagerImpl",
                    sample_message=message, first_seen=NOW, last_seen=NOW, occurrence_count=4)


# ------------------------------------------------------------------ fakes

DEFAULTS = {
    "get_pod_status": [
        {"pod": "pingfederate-pingfederate-admin-abc", "phase": "Running", "ready": "2/2", "containers": [{"restart_count": 1}]},
        {"pod": "pingfederate-pingfederate-engine-def", "phase": "Running", "ready": "2/2", "containers": [{"restart_count": 0}]},
        {"pod": "postgres-0", "phase": "Running", "ready": "1/1", "containers": [{"restart_count": 0}]}],
    "list_baseline": [{"key": ADMIN_KEY, "golden_value": GOLD, "source_file": VALUES}, {"key": ENGINE_KEY, "golden_value": GOLD, "source_file": VALUES}],
    "get_live_config_value": lambda a: {"value": BAD},
    "get_pf_license": {"days_until_expiry": 106},
    "get_pf_certificates": {"ssl_server": [{"days_until_expiry": 3600}], "signing": [], "ca": []},
    "get_pf_cluster_status": {"nodes": [{"replicationStatus": "SUCCEEDED"}], "replicationRequired": False},
    "get_logs": [{"message": "x"}] * 4,
    "get_baseline_value": lambda a: {"key": a["key"], "golden_value": GOLD},
    "search_vector": [{"title": "JDBC datastore failure", "related_keys": [ADMIN_KEY, ENGINE_KEY]}],
    "get_pf_datastore": {"name": "PostgresPfApp", "connectionUrl": BAD},
    "read_values_yaml_key": {"value": BAD, "file_sha256": "sha0", "line": 1447},
    "patch_helm_values": {"new_file_sha256": "sha1", "unified_diff": "-bad\n+good"},
    "helm_render_validate": {"valid": True, "errors": [], "patch_changed_lines": 4, "rendered_diff": "diff"},
    "push_remediation_branch": {"branch": "remediation/inc-7"},
    "create_pull_request": {"url": "https://github.com/x/y/pull/14", "number": 14},
    "add_pr_comment": {},
    "create_github_issue": {"url": "https://github.com/x/y/issues/15", "number": 15},
}


class FakeTools:
    def __init__(self, responses=None, fail=None):
        self.responses = {**DEFAULTS, **(responses or {})}
        self.fail = fail or {}
        self.calls: list[tuple[str, dict]] = []
        self.on_call = None

    async def call(self, name, args=None, timeout=None, traced=True):
        self.calls.append((name, args or {}))
        if name in self.fail:
            if self.on_call:
                self.on_call(name, args or {}, None, self.fail[name], 1)
            raise ToolFailed(self.fail[name])
        r = self.responses[name]
        value = r(args or {}) if callable(r) else r
        if self.on_call:
            self.on_call(name, args or {}, value, None, 1)
        return value

    def read_tools(self):
        def make(name):
            async def run(**kw):
                return json.dumps(await self.call(name, kw))
            return StructuredTool(name=name, description=name, args_schema={"type": "object", "properties": {}}, coroutine=run)
        return [make(n) for n in ("read_values_yaml_key", "get_pf_datastore", "search_vector", "get_baseline_value")]

    def names(self):
        return [n for n, _ in self.calls]


class ScriptedLLM:
    """Stands in for the chat model: returns the next scripted reply each turn."""
    def __init__(self, replies):
        self.replies, self.forced_turns, self.turns = list(replies), 0, 0

    def bind_tools(self, tools, **kw):
        return _Bound(self, forced=kw.get("tool_choice") == "any")


class _Bound:
    def __init__(self, parent, forced):
        self.parent, self.forced = parent, forced

    async def ainvoke(self, messages):
        self.parent.turns += 1
        self.parent.forced_turns += self.forced
        return self.parent.replies.pop(0)


def tool_call(name, args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def fix_args(value=GOLD, evidence=("e2",), keys=(ADMIN_KEY, ENGINE_KEY), confidence="high"):
    return {"file": VALUES, "changes": [{"key_path": k, "new_value": value} for k in keys], "root_cause": "JDBC URL points at a nonexistent host",
            "confidence": confidence, "evidence_ids": list(evidence), "explanation": "live, repo and golden disagree"}


def escalate_args():
    return {"root_cause": "unknown", "confidence": "low", "evidence_ids": ["e1"], "checked": ["pods"], "unknown": ["cause"],
            "next_step": "look at it", "explanation": "could not tell"}


class MemoryLog(NullRunLog):
    """Keeps events in a list so tests can read the record."""
    def __init__(self):
        self.events, self.node, self.logged_messages = [], "-", 0

    def event(self, kind, data=None, node=None, duration_ms=None):
        self.events.append({"kind": kind, "node": node or self.node, "data": data or {}, "ms": duration_ms})

    def tool_call(self, name, args, result, error, duration_ms):
        self.event("tool_call", {"tool": name, "args": args, "result": result, "error": error}, duration_ms=duration_ms)

    def kinds(self):
        return [e["kind"] for e in self.events]


def run(tools, replies, inc=None, max_steps=8, log=None):
    llm = ScriptedLLM(replies)
    settings = Settings(max_steps=max_steps)
    graph = IamAgent(tools, llm, settings, log).build()
    final = asyncio.run(graph.ainvoke(AgentState(incident=inc or incident()), config={"recursion_limit": 80}))
    return AgentState(**final), llm


# ------------------------------------------------------------------ whole-graph runs

def test_happy_path_opens_a_pr_in_the_right_order():
    tools = FakeTools()
    state, llm = run(tools, [tool_call("read_values_yaml_key", {"key_path": ADMIN_KEY}), tool_call("propose_repo_fix", fix_args(), "c2")])
    assert state.outcome.kind == "pr_opened" and state.outcome.url.endswith("/pull/14")
    assert state.policy.passed and state.decision.kind == "repo_fix"
    names = tools.names()
    write_order = [n for n in names if n in ("patch_helm_values", "helm_render_validate", "push_remediation_branch", "create_pull_request", "add_pr_comment")]
    assert write_order == ["patch_helm_values", "patch_helm_values", "helm_render_validate", "push_remediation_branch", "create_pull_request", "add_pr_comment"]
    patches = [a for n, a in tools.calls if n == "patch_helm_values"]
    assert patches[0]["expected_file_sha256"] == "sha0" and patches[1]["expected_file_sha256"] == "sha1"   # the sha is chained
    assert "create_github_issue" not in names


def test_evidence_is_numbered_after_intake_and_cited_by_the_decision():
    state, _ = run(FakeTools(), [tool_call("read_values_yaml_key", {"key_path": ADMIN_KEY}), tool_call("propose_repo_fix", fix_args(), "c2")])
    ids = [e.id for e in state.evidence]
    assert ids == [f"e{i}" for i in range(1, len(ids) + 1)]
    assert state.evidence[-1].tool == "read_values_yaml_key"      # the LLM's own call comes after intake's
    assert state.snapshot.drift[0].state == "drift"


def test_a_value_that_is_not_golden_is_blocked_and_nothing_is_written():
    tools = FakeTools()
    state, _ = run(tools, [tool_call("propose_repo_fix", fix_args(value="jdbc:postgresql://postgres-new:5432/x"))])
    assert state.policy.failed_check == 3 and not state.policy.passed
    assert state.decision.kind == "escalate" and state.outcome.kind == "escalated"
    assert not {"patch_helm_values", "push_remediation_branch", "create_pull_request"} & set(tools.names())
    assert "create_github_issue" in tools.names()


def test_citing_evidence_that_does_not_exist_is_blocked():
    state, _ = run(FakeTools(), [tool_call("propose_repo_fix", fix_args(evidence=("e99",)))])
    assert state.policy.failed_check == 7 and state.outcome.kind == "escalated"


def test_a_secret_key_is_blocked():
    state, _ = run(FakeTools(), [tool_call("propose_repo_fix", fix_args(keys=("pingfederate-admin.envs.POSTGRES_JDBC_PASSWORD",)))])
    assert state.policy.failed_check == 2


def test_low_confidence_fix_is_downgraded():
    state, _ = run(FakeTools(), [tool_call("propose_repo_fix", fix_args(confidence="low"))])
    assert state.policy.failed_check == 6 and state.outcome.kind == "escalated"


def test_a_pr_already_in_flight_blocks_a_second_one():
    state, _ = run(FakeTools(), [tool_call("propose_repo_fix", fix_args())], inc=incident(status="WAITING_FOR_PR_REVIEW"))
    assert state.policy.failed_check == 8


def test_escalate_decision_goes_straight_to_an_issue():
    tools = FakeTools()
    state, _ = run(tools, [tool_call("escalate", escalate_args())])
    assert state.outcome.kind == "escalated" and state.policy.passed
    assert state.policy.rows[0].result == "na"     # repo checks do not apply to an escalation


def test_when_the_tool_budget_is_spent_the_llm_is_forced_to_decide():
    reads = [tool_call("search_vector", {"query": "x"}, f"c{i}") for i in range(3)]
    state, llm = run(FakeTools(), reads + [tool_call("escalate", escalate_args(), "cz")], max_steps=3)
    assert state.steps_used == 3 and llm.forced_turns == 1
    assert state.outcome.kind == "escalated"


def test_an_invalid_decision_is_sent_back_and_a_valid_one_accepted():
    bad = fix_args(); del bad["confidence"]
    state, llm = run(FakeTools(), [tool_call("propose_repo_fix", bad), tool_call("propose_repo_fix", fix_args(), "c2")])
    assert llm.turns == 2 and state.outcome.kind == "pr_opened"
    assert any("invalid arguments" in str(m.content) for m in state.messages)


def test_an_agent_that_never_decides_is_stopped():
    state, llm = run(FakeTools(), [AIMessage(content="thinking") for _ in range(20)], max_steps=2)
    assert state.outcome.kind == "escalated" and llm.turns <= 2 + 5 + 1


def test_a_failed_render_check_escalates_and_nothing_is_pushed():
    tools = FakeTools({"helm_render_validate": {"valid": False, "errors": ["bad yaml"], "patch_changed_lines": 0, "rendered_diff": ""}})
    state, _ = run(tools, [tool_call("propose_repo_fix", fix_args())])
    assert state.outcome.kind == "escalated" and "render check failed" in state.decision.next_step
    assert "push_remediation_branch" not in tools.names() and "create_pull_request" not in tools.names()


def test_a_failing_write_tool_escalates_with_its_reason():
    tools = FakeTools(fail={"patch_helm_values": "values.yaml changed since it was read"})
    state, _ = run(tools, [tool_call("propose_repo_fix", fix_args())])
    assert state.outcome.kind == "escalated" and "changed since it was read" in state.decision.next_step


def test_a_failed_issue_is_reported_not_raised():
    tools = FakeTools(fail={"create_github_issue": "gh not logged in"})
    state, _ = run(tools, [tool_call("escalate", escalate_args())])
    assert state.outcome.kind == "escalated" and state.outcome.url is None and "gh not logged in" in state.outcome.detail


def test_the_llm_is_never_given_a_write_tool():
    assert not READ_TOOLS & {"patch_helm_values", "patch_server_profile", "push_remediation_branch", "create_pull_request",
                             "add_pr_comment", "create_github_issue", "helm_upgrade_release", "helm_rollback_release", "list_baseline"}


# ------------------------------------------------------------------ intake

def test_intake_finds_the_drifted_keys():
    snap, evidence = asyncio.run(build_snapshot(FakeTools(), incident()))
    assert [d.state for d in snap.drift] == ["drift", "drift"]
    assert snap.license_days == 106 and snap.cert_days == 3600 and snap.replication == "SUCCEEDED" and snap.recent_errors == 4
    assert [e.id for e in evidence] == ["e1", "e2", "e3", "e4", "e5"]
    assert "2 of 2 baseline keys differ" in snap.summary()


def test_intake_survives_an_unreachable_source():
    snap, evidence = asyncio.run(build_snapshot(FakeTools(fail={"get_pod_status": "cluster down", "get_pf_license": "PF down"}), incident()))
    assert any("get_pod_status" in u for u in snap.unavailable) and any("get_pf_license" in u for u in snap.unavailable)
    assert snap.license_days is None and len(evidence) == 5          # still produces its evidence


def test_intake_reports_matching_keys_as_ok():
    snap, _ = asyncio.run(build_snapshot(FakeTools({"get_live_config_value": lambda a: {"value": GOLD}}), incident()))
    assert {d.state for d in snap.drift} == {"ok"}


# ------------------------------------------------------------------ policy, one check at a time

def make_fix(**kw):
    d = dict(file=VALUES, changes=[Change(key_path=ADMIN_KEY, new_value=GOLD)],
             diagnosis=Diagnosis(root_cause="r", confidence="high", evidence_ids=["e1"], explanation="x"))
    d.update(kw)
    return RepoFix(**d)


@pytest.mark.parametrize("fix, golden, n", [
    (make_fix(file="infrastructure/main.tf"), {ADMIN_KEY: GOLD}, 1),
    (make_fix(changes=[Change(key_path="a.b.API_TOKEN", new_value="x")]), {"a.b.API_TOKEN": "x"}, 2),
    (make_fix(changes=[Change(key_path=ADMIN_KEY, new_value="other")]), {ADMIN_KEY: GOLD}, 3),
    (make_fix(), {}, 3),                                   # no baseline entry: cannot confirm it is known-good
    (make_fix(changes=[Change(key_path=f"p.envs.K{i}", new_value="v") for i in range(4)]), {f"p.envs.K{i}": "v" for i in range(4)}, 4),
    (make_fix(diagnosis=Diagnosis(root_cause="r", confidence="low", evidence_ids=["e1"], explanation="x")), {ADMIN_KEY: GOLD}, 6),
    (make_fix(diagnosis=Diagnosis(root_cause="r", confidence="high", evidence_ids=[], explanation="x")), {ADMIN_KEY: GOLD}, 7),
])
def test_each_policy_check_can_fail(fix, golden, n):
    result = run_policy(fix, {"e1"}, incident(), golden)
    assert not result.passed and result.failed_check == n
    assert all(r.result == "skip" for r in result.rows if r.number > n and r.result != "na")   # the first failure stops the run


def test_policy_passes_a_clean_fix_and_lists_all_ten_checks():
    result = run_policy(make_fix(), {"e1"}, incident(), {ADMIN_KEY: GOLD})
    assert result.passed and len(result.rows) == 10
    assert [r.number for r in result.rows if r.result == "na"] == [5, 9]


# ------------------------------------------------------------------ prompts, trigger, toolbox

def test_every_prompt_renders_and_a_missing_value_fails_loudly():
    values = {k: "x" for name in ("system", "trigger_message", "forced_decision", "escalation_report", "pr_body")
              for k in models.placeholders(name)}
    for name in ("system", "trigger_message", "forced_decision", "escalation_report", "pr_body"):
        text = models.render(name, **values)
        assert "{" not in text or name == "pr_body"      # pr_body holds a literal code fence only
    with pytest.raises(KeyError):
        models.render("system")


def test_parse_result_handles_lists_dicts_text_and_errors():
    tb = lambda **kw: NS(is_error=False, structured_content=None, content=[], **kw)
    assert parse_result(NS(is_error=False, structured_content={"result": [1, 2]}, content=[])) == [1, 2]
    assert parse_result(tb()) is None
    assert parse_result(NS(is_error=False, structured_content=None, content=[NS(text='{"a": 1}')])) == {"a": 1}
    assert parse_result(NS(is_error=False, structured_content=None, content=[NS(text="plain")])) == "plain"
    with pytest.raises(ToolFailed, match="because"):
        parse_result(NS(is_error=True, structured_content=None, content=[NS(text="because")]))


def test_real_toolbox_over_mcp_gives_reads_to_the_llm_and_reasons_for_refusals():
    """Starts the real repo_read server (needs only git, no cluster)."""
    async def go():
        settings = Settings.from_env()
        async with Toolbox(settings, servers={"repo_read": "mcp_servers/repo_read/server.py"}) as tb:
            names = {t.name for t in tb.read_tools()}
            found = await tb.call("find_in_repo", {"text": "POSTGRES_JDBC_URL"}, timeout=30.0)    # the real client accepts a timeout in seconds
            refused = await [t for t in tb.read_tools() if t.name == "read_values_yaml_key"][0].ainvoke({"key_path": "no.such.key"})
            with pytest.raises(ToolFailed, match="key not found"):
                await tb.call("read_values_yaml_key", {"key_path": "no.such.key"})
            return names, found, refused
    names, found, refused = asyncio.run(go())
    assert names == {"read_values_yaml_key", "read_server_profile_file", "diff_vs_golden", "find_in_repo"}
    assert any(m["key_path"] == ADMIN_KEY for m in found["matches"])
    assert refused.startswith("ERROR:") and "key not found" in refused


def test_settings_read_the_env_file_but_never_override_the_shell(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("AGENT_DB_DSN=from-file\nPF_ADMIN_PASSWORD=pw-from-file\nAGENT_MODEL=model-from-file\n")
    monkeypatch.delenv("AGENT_DB_DSN", raising=False)
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.setenv("PF_ADMIN_PASSWORD", "from-shell")
    s = Settings.from_env(env_file)
    assert s.mcp_env["AGENT_DB_DSN"] == "from-file"          # filled in from the file
    assert s.mcp_env["PF_ADMIN_PASSWORD"] == "from-shell"    # the shell wins
    assert s.model == "model-from-file"


# ------------------------------------------------------------------ the run log

def logged_run(replies, **kw):
    log = MemoryLog()
    state, llm = run(FakeTools(**kw), replies, log=log)
    return log, state


def test_the_run_log_records_every_step_of_a_successful_run():
    log, state = logged_run([tool_call("read_values_yaml_key", {"key_path": ADMIN_KEY}), tool_call("propose_repo_fix", fix_args(), "c2")])
    kinds = log.kinds()
    for needed in ("snapshot", "prompt", "evidence", "llm_call", "tool_call", "decision", "policy", "outcome", "node_start", "node_end"):
        assert needed in kinds, needed
    nodes_started = [e["node"] for e in log.events if e["kind"] == "node_start"]
    assert nodes_started == ["intake", "agent", "run_tools", "agent", "record_decision", "policy", "apply_fix"]
    assert all(e["ms"] is not None for e in log.events if e["kind"] == "node_end")


def test_the_run_log_keeps_tool_arguments_and_full_results_including_code_node_calls():
    log, _ = logged_run([tool_call("read_values_yaml_key", {"key_path": ADMIN_KEY}), tool_call("propose_repo_fix", fix_args(), "c2")])
    calls = {}
    for e in log.events:
        if e["kind"] == "tool_call":
            calls.setdefault(e["data"]["tool"], []).append(e["data"])
    assert calls["read_values_yaml_key"][0]["args"] == {"key_path": ADMIN_KEY}           # the LLM's call
    assert calls["read_values_yaml_key"][0]["result"]["file_sha256"] == "sha0"
    assert [c["args"]["expected_file_sha256"] for c in calls["patch_helm_values"]] == ["sha0", "sha1"]   # a code node's calls
    assert calls["create_pull_request"][0]["result"]["number"] == 14
    assert {"get_pod_status", "list_baseline", "get_live_config_value"} <= calls.keys()   # intake's calls too


def test_the_run_log_records_what_each_llm_call_was_shown_without_repeating_history():
    log, _ = logged_run([tool_call("read_values_yaml_key", {"key_path": ADMIN_KEY}), tool_call("propose_repo_fix", fix_args(), "c2")])
    calls = [e["data"] for e in log.events if e["kind"] == "llm_call"]
    assert [m["role"] for m in calls[0]["new_input"]] == ["system", "human"]
    assert [m["role"] for m in calls[1]["new_input"]] == ["tool"]                    # only the new tool result
    assert calls[0]["output"]["tool_calls"][0]["name"] == "read_values_yaml_key"
    assert calls[1]["output"]["tool_calls"][0]["name"] == "propose_repo_fix"


def test_the_run_log_records_the_policy_rows_and_the_decision_even_when_blocked():
    log, _ = logged_run([tool_call("propose_repo_fix", fix_args(value="jdbc:postgresql://other:5432/x"))])
    policy = next(e["data"] for e in log.events if e["kind"] == "policy")
    assert policy["passed"] is False and policy["failed_check"] == 3 and len(policy["rows"]) == 10
    decision = next(e["data"] for e in log.events if e["kind"] == "decision")
    assert decision["kind"] == "repo_fix" and decision["diagnosis"]["confidence"] == "high"   # what the model proposed, before the downgrade
    assert [e for e in log.events if e["kind"] == "outcome"][0]["data"]["kind"] == "escalated"


def test_a_crash_inside_a_node_is_logged_before_it_propagates():
    log = MemoryLog()
    tools = FakeTools({"get_pod_status": lambda a: (_ for _ in ()).throw(RuntimeError("boom"))})
    with pytest.raises(Exception):
        run(tools, [tool_call("escalate", escalate_args())], log=log)
    err = [e for e in log.events if e["kind"] == "error"]
    assert err and "boom" in err[0]["data"]["error"] and err[0]["node"] == "intake"


def test_a_database_failure_in_the_log_never_breaks_the_agent(capsys):
    from agent.records import RunLog
    log = RunLog(7, "m", dsn="postgresql://nobody@127.0.0.1:1/none?connect_timeout=1")   # unreachable
    log.event("evidence", {"x": 1})
    log.finish("escalated")
    assert "could not write to Postgres" in capsys.readouterr().err


def test_pr_sync_maps_what_github_says_to_an_incident_status(monkeypatch):
    from agent import records
    calls = []
    monkeypatch.setattr(records, "set_status", lambda i, s: calls.append((i, s)))

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): return NS(fetchall=lambda: [(1, "u/1"), (2, "u/2"), (3, "u/3"), (4, None)])
    monkeypatch.setattr(records.psycopg, "connect", lambda *a, **k: FakeConn())
    states = {"u/1": "MERGED", "u/2": "CLOSED", "u/3": "OPEN"}
    changed = records.sync_pr_status(get_state=lambda url: states[url])
    assert changed == [(1, "MERGED"), (2, "PR_REJECTED")] and calls == changed      # open PRs and missing urls are left alone


def test_tracing_is_off_unless_asked_and_tool_spans_never_break_a_call(monkeypatch):
    from agent import records as tracing
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)
    assert tracing.setup_tracing() is False
    with tracing.tool_span("some_tool", {"a": 1}) as record:      # no-op or real span: either way it must not raise
        record({"ok": True}, None)
        record(None, "failed")


# ------------------------------------------------------------------ deploy after merge, and startup noise

from agent import deploy  # noqa: E402
from agent.signature import is_simulated_fault  # noqa: E402

READY_PODS = [{"pod": "pingfederate-pingfederate-admin-x", "phase": "Running", "ready": "2/2", "containers": [{"restart_count": 0}]},
              {"pod": "pingfederate-pingfederate-engine-y", "phase": "Running", "ready": "2/2", "containers": [{"restart_count": 0}]},
              {"pod": "postgres-0", "phase": "Running", "ready": "1/1", "containers": [{"restart_count": 0}]}]


async def _no_sleep(_):
    return None


def run_deploy_fake(responses=None, fail=None, **timing):
    tools = FakeTools({"helm_upgrade_release": {"revision": 26, "status": "deployed", "commit": "abc"},
                       # revision 25 failed halfway; the revision that was actually running is 24
                       "get_helm_release": {"history": [{"revision": 24, "status": "deployed"}, {"revision": 25, "status": "failed"}]},
                       "helm_rollback_release": {"revision": 25, "status": "deployed"},
                       "get_pod_status": READY_PODS, "get_live_config_value": lambda a: {"value": GOLD},
                       "get_logs": [], **(responses or {})}, fail=fail)
    log = MemoryLog()
    result = asyncio.run(deploy.deploy_and_verify(tools, "abc123", "tok", log, sleep=_no_sleep, observe=0, poll=0, **timing))
    return result, tools, log


def test_a_good_deploy_is_verified_and_resolved():
    result, tools, log = run_deploy_fake()
    assert result.status == "resolved" and result.revision == 26
    assert "helm_rollback_release" not in tools.names()
    assert ("helm_upgrade_release", {"commit_sha": "abc123", "token": "tok"}) in tools.calls      # deploys the exact commit
    assert log.kinds()[0] == "deploy_start" and "verify" in log.kinds()


def test_if_the_config_still_drifts_after_the_deploy_it_rolls_back_to_the_previous_revision():
    result, tools, _ = run_deploy_fake({"get_live_config_value": lambda a: {"value": BAD}})
    assert result.status == "rolled_back" and "still differ" in result.detail
    assert ("helm_rollback_release", {"revision": 24, "token": "tok"}) in tools.calls      # the revision running BEFORE the upgrade, not 26 - 1


def test_if_the_error_comes_back_it_rolls_back():
    result, tools, _ = run_deploy_fake({"get_logs": [{"message": "Unable to load custom data source instance"}]})
    assert result.status == "rolled_back" and "appeared again" in result.detail and "helm_rollback_release" in tools.names()


def test_pods_that_never_become_ready_fail_the_check():
    not_ready = [{**READY_PODS[0], "ready": "0/2"}] + READY_PODS[1:]
    result, tools, _ = run_deploy_fake({"get_pod_status": not_ready}, wait=0)
    assert result.status == "rolled_back" and "not all ready" in result.detail


def test_a_failed_upgrade_is_failed_without_a_rollback_because_helm_already_undid_it():
    result, tools, _ = run_deploy_fake(fail={"helm_upgrade_release": "timed out waiting for the condition"})
    assert result.status == "failed" and "helm_rollback_release" not in tools.names()


def test_a_failed_rollback_is_reported_as_failed():
    result, _, _ = run_deploy_fake({"get_live_config_value": lambda a: {"value": BAD}}, fail={"helm_rollback_release": "no such revision"})
    assert result.status == "failed" and "no such revision" in result.detail


def test_pods_ready_needs_every_pod_running_and_fully_ready():
    assert deploy.pods_ready(READY_PODS)
    assert not deploy.pods_ready([{**READY_PODS[0], "phase": "Pending", "ready": "0/2"}])
    assert not deploy.pods_ready([])


def test_the_agents_own_toolbox_never_includes_the_deploy_server():
    from agent.tools import SERVER_SCRIPTS
    assert "helm_deploy" not in SERVER_SCRIPTS and "helm_deploy" in deploy.DEPLOY_SERVERS


@pytest.mark.parametrize("logger, message, expected", [
    ("org.sourceid.saml20.domain.mgmt.impl.DataSourceManagerImpl", "Unable to load custom data source instance: JDBC-FD67", True),   # Sim A
    (None, "Unable to load custom data source instance: JDBC-FD67494D", True),
    ("org.jgroups.protocols.pbcast.GMS", "10.244.0.16:7600: too many JOIN attempts (10): becoming singleton", False),               # cluster chatter
    ("x", "pf.runtime.threads.max is not set. Bulkhead manager will not be enabled.", False),                                         # startup warning
    ("com.pingidentity.jdbc.Pool", "Connection refused to jdbc:postgresql://10.244.0.43:5432/postgres after 30000ms", False),         # not a simulated fault
    (None, "", False),
])
def test_only_the_simulated_fault_becomes_an_incident(logger, message, expected):
    assert is_simulated_fault(logger, message) is expected


def test_every_entry_point_imports_the_way_it_is_started():
    """The worker is started as `python -m agent.worker`; a bad import there stops the whole demo."""
    import importlib, subprocess
    for module in ("agent.worker", "agent.deploy", "agent.graph"):
        importlib.import_module(module)
    out = subprocess.run([sys.executable, "-m", "agent.worker", "--help"], capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent)
    assert out.returncode == 0, out.stderr[-400:]
