import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ui.timeline import build_timeline, describe_tool  # noqa: E402

T = datetime(2026, 10, 20, tzinfo=timezone.utc)
INC = {"status": "WAITING_FOR_PR_REVIEW", "sample_message": "Unable to load custom data source instance: JDBC-1", "first_seen": T}


def ev(kind, data, node="x"):
    return {"ts": T, "node": node, "kind": kind, "data": data}


RUN = {"started_at": T, "model": "claude-haiku-5-5", "status": "pr_opened", "outcome": None, "error": None, "events": [
    ev("snapshot", {"drift": [{"key": "pingfederate-admin.envs.POSTGRES_JDBC_URL", "state": "drift"}, {"key": "k2", "state": "ok"}]}, "intake"),
    ev("tool_call", {"tool": "read_values_yaml_key", "args": {"key_path": "pingfederate-admin.envs.POSTGRES_JDBC_URL"}, "result": {"value": "jdbc:bad"}, "error": None}, "run_tools"),
    ev("decision", {"kind": "repo_fix", "diagnosis": {"root_cause": "URL points at a nonexistent host", "confidence": "high"}}),
    ev("policy", {"passed": True}),
    ev("tool_call", {"tool": "patch_helm_values", "result": {"branch": "remediation/inc-1", "commit": "abcdef1234"}}, "apply_fix"),
    ev("tool_call", {"tool": "create_pull_request", "result": {"number": 4, "url": "https://github.com/x/y/pull/4"}}, "apply_fix"),
]}


def titles(steps):
    return [s["title"] for s in steps]


def test_a_pr_waiting_for_merge_reads_as_a_story_with_the_next_steps_greyed():
    steps = build_timeline(INC, [RUN])
    assert titles(steps)[:2] == ["Error detected", "Agent started"]
    assert "Opened pull request #4" in titles(steps) and "Safety check passed" in titles(steps)
    tail = steps[-3:]
    assert [s["state"] for s in tail] == ["active", "pending", "pending"] and tail[0]["title"].startswith("Waiting for you to merge")
    assert next(s for s in steps if s["title"] == "Took a snapshot of the system")["detail"].startswith("1 of 2 config keys differ")


def test_tool_calls_are_described_in_plain_words_and_errors_are_kept():
    assert describe_tool("read_values_yaml_key", {"key_path": "a.b.URL"}, {"value": "v"}, None) == ("Read URL in values.yaml on GitHub", "v")
    what, found = describe_tool("get_live_config_value", {"key": "K"}, None, "pod not found")
    assert "Tried" in what and "pod not found" in found
    assert describe_tool("unknown_tool", {}, {"a": 1}, None)[0] == "Used unknown_tool"


def test_a_resolved_incident_ends_with_resolved_and_nothing_pending():
    run = {**RUN, "events": RUN["events"] + [ev("deploy_start", {"commit": "abc1234def"}, "deploy"), ev("verify", {"ok": True, "detail": "all pods ready"}, "deploy"),
                                           ev("deploy_result", {"status": "resolved", "detail": "ok"}, "deploy")]}
    steps = build_timeline({**INC, "status": "RESOLVED"}, [run])
    assert steps[-1]["title"] == "Resolved. The fix is live" and steps[-1]["state"] == "done"
    assert not [s for s in steps if s["state"] in ("active", "pending")]


def test_a_failed_check_is_marked_bad_and_a_retry_adds_an_attempt_header():
    bad = {**RUN, "events": RUN["events"] + [ev("verify", {"ok": False, "detail": "2 keys still differ"}, "deploy"),
                                           ev("deploy_result", {"status": "rolled_back", "detail": "x"}, "deploy")]}
    steps = build_timeline({**INC, "status": "RETRY"}, [bad, RUN])
    assert "Attempt 1" in titles(steps) and "Attempt 2" in titles(steps)
    assert [s["state"] for s in steps if s["title"] in ("Checked the result", "Check failed. Rolled back")] == ["bad", "bad"]
    assert steps[-1]["title"].startswith("Waiting for you to merge") is False


def test_a_blocked_fix_and_an_escalation_are_shown():
    blocked = {**RUN, "events": [ev("decision", {"kind": "repo_fix", "diagnosis": {"root_cause": "r", "confidence": "high"}}),
                               ev("policy", {"passed": False, "failed_check": 3, "reason": "not golden"}),
                               ev("outcome", {"kind": "escalated", "url": "https://github.com/x/y/issues/5", "detail": "issue opened"}, "escalate")]}
    steps = build_timeline({**INC, "status": "ESCALATED"}, [blocked])
    assert any(s["title"] == "Safety check blocked the fix" and s["state"] == "bad" for s in steps)
    assert any(s["title"] == "Opened a GitHub issue for a human" and s["url"].endswith("/issues/5") for s in steps)


def test_before_the_error_is_logged_and_with_no_fault_at_all():
    assert build_timeline(None, [], waiting_for_error=True)[0]["state"] == "active"
    assert build_timeline(None, [], waiting_for_error=False) == []
    assert build_timeline({**INC, "status": "NEW"}, [])[-1]["title"] == "Starting the agent"


def test_changing_two_keys_is_one_line_not_two():
    steps = build_timeline(INC, [{**RUN, "events": RUN["events"][:4] + [
        ev("tool_call", {"tool": "patch_helm_values", "result": {"branch": "remediation/inc-1", "commit": "abcdef1234"}}, "apply_fix"),
        ev("tool_call", {"tool": "patch_helm_values", "result": {"branch": "remediation/inc-1", "commit": "abcdef1234"}}, "apply_fix")]}])
    prepared = [s for s in steps if s["title"] == "Prepared the fix on a branch"]
    assert len(prepared) == 1 and prepared[0]["detail"].startswith("2 keys changed")
