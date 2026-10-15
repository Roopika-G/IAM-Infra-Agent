"""The policy check: plain code that decides whether a decision may run.

The LLM can decide anything. Nothing runs unless these checks pass. Checks
run in order and the first failure stops the run; the decision is then
downgraded to an escalation (see graph.py) with the reason attached.

Which checks apply depends on the kind of decision:
  repo_fix : 1 path, 2 key, 3 value, 4 size, 6 confidence, 7 evidence, 8 duplicate, 10 topic
  escalate : 7 evidence, 8 duplicate, 10 topic
Checks 5 (command allowlist) and 9 (action budget) arrive with runtime actions.
"""

import re

from mcp_servers import common

from agent.models import CheckResult, Confidence, Escalation, Incident, PolicyResult, RepoFix

NAMES = {
    1: "Path allowed", 2: "Key is not secret-looking", 3: "New value is a known-good value", 4: "Change is small",
    5: "Command is on the allowlist", 6: "Confidence is not low", 7: "Cited evidence exists",
    8: "No duplicate in flight", 9: "Action budget and lock", 10: "Not an escalate-only topic",
}
APPLIES = {"repo_fix": [1, 2, 3, 4, 6, 7, 8, 10], "escalate": [7, 8, 10]}
MAX_KEYS = 3
IN_FLIGHT = {"PR_CREATED", "WAITING_FOR_PR_REVIEW"}
_ESCALATE_ONLY = re.compile(r"license|jwk|secret|private|certificate|keypair", re.IGNORECASE)


def _check(n: int, decision, evidence_ids: set[str], incident: Incident, golden: dict[str, str | None]) -> str | None:
    """Return None if check n passes, else the reason it failed."""
    if n == 1:
        if decision.file != common.VALUES_FILE:
            return f"{decision.file!r} is not {common.VALUES_FILE}"
        try:
            common.validate_repo_path(decision.file)
        except ValueError as e:
            return str(e)
    elif n == 2:
        for c in decision.changes:
            if common.is_protected_key(c.key_path.split(".")[-1]):
                return f"{c.key_path} looks like a secret, license or key; never auto-patched"
    elif n == 3:
        for c in decision.changes:
            known = golden.get(c.key_path)
            if known is None:
                return f"no baseline value for {c.key_path}; cannot confirm the new value is known-good"
            if c.new_value != known:
                return f"new value for {c.key_path} does not equal the baseline golden value"
    elif n == 4:
        if len(decision.changes) > MAX_KEYS:
            return f"{len(decision.changes)} keys changed (limit {MAX_KEYS})"
    elif n == 6:
        if decision.diagnosis.confidence == Confidence.low:
            return "confidence is low"
    elif n == 7:
        cited = decision.diagnosis.evidence_ids
        if not cited:
            return "no evidence cited"
        missing = [e for e in cited if e not in evidence_ids]
        if missing:
            return f"cites evidence that does not exist: {missing}"
    elif n == 8:
        if incident.status in IN_FLIGHT:
            return f"incident #{incident.id} already has a pull request in flight"
    elif n == 10:
        if isinstance(decision, RepoFix):
            text = " ".join([decision.file] + [c.key_path for c in decision.changes])
            if _ESCALATE_ONLY.search(text):
                return "touches a secret, license or certificate topic, which is escalate-only"
    return None


def pass_note(n: int, decision) -> str:
    if n == 1:
        return f"{decision.file} is on the allowlist"
    if n == 2:
        return "no secret pattern in " + ", ".join(c.key_path.split(".")[-1] for c in decision.changes)
    if n == 3:
        return "every new value equals the baseline golden value"
    if n == 4:
        return f"{len(decision.changes)} key(s) changed (limit {MAX_KEYS})"
    if n == 6:
        return decision.diagnosis.confidence.value
    if n == 7:
        return "every cited evidence id exists"
    if n == 8:
        return "no pull request in flight for this incident"
    return "not a secret, license or certificate topic"


def run_policy(decision: RepoFix | Escalation, evidence_ids: set[str], incident: Incident, golden: dict[str, str | None]) -> PolicyResult:
    applies = APPLIES[decision.kind]
    rows: list[CheckResult] = []
    failed: int | None = None
    reason: str | None = None
    for n in range(1, 11):
        if n not in applies:
            rows.append(CheckResult(number=n, name=NAMES[n], result="na", note="does not apply to this kind of decision"))
        elif failed is not None:
            rows.append(CheckResult(number=n, name=NAMES[n], result="skip", note="not run: an earlier check already failed"))
        else:
            problem = _check(n, decision, evidence_ids, incident, golden)
            if problem:
                failed, reason = n, problem
                rows.append(CheckResult(number=n, name=NAMES[n], result="fail", note=problem))
            else:
                rows.append(CheckResult(number=n, name=NAMES[n], result="pass", note=pass_note(n, decision)))
    return PolicyResult(passed=failed is None, kind=decision.kind, failed_check=failed, reason=reason, rows=rows)
