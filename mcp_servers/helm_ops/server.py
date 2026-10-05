"""helm_ops MCP server: the agent-facing, read-only half of Helm.

  helm_render_validate -- dry-run: render the chart from main and from an
                          incident's worktree branch, and diff the two

Deliberately has NO upgrade or rollback tool -- those live in helm_deploy,
a separate server only CI/CD is pointed at (plan.md Phase 9/12). The agent
can preview a patch; it can never apply one.

Run: uv run python mcp_servers/helm_ops/server.py
"""

import difflib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mcp_servers import common  # noqa: E402

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-self-healing-iam")
NAMESPACE = "pingfederate"
RELEASE = "pingfederate"
BASE_REF = "main"
MAX_DIFF_CHARS = 20000

mcp = MCPServer("helm_ops")


def _helm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["helm", *args], capture_output=True, text=True, timeout=120)


def _diff(a: str, b: str, from_name: str, to_name: str) -> tuple[int, str]:
    lines = list(difflib.unified_diff(a.splitlines(), b.splitlines(), fromfile=from_name, tofile=to_name, lineterm="", n=2))
    changed = sum(1 for l in lines if l[:1] in "+-" and not l.startswith(("+++", "---")))
    return changed, "\n".join(lines)


@mcp.tool()
def helm_render_validate(incident_id: int) -> dict:
    """Render helm/ping-devops twice -- from main, and from the worktree
    branch remediation/inc-<id> (created by repo_config) -- and diff the two,
    so the result shows what THIS PATCH changes and nothing else. Changes
    nothing. Returns {valid, errors, patch_changed_lines, rendered_diff,
    deployed_differs_from_main_lines}. valid is false if the branch doesn't
    render. deployed_differs_from_main_lines counts how far the live release
    already is from main before any patch (non-zero means the cluster is not
    exactly what main describes)."""
    if not isinstance(incident_id, int) or incident_id <= 0:
        raise ValueError("incident_id must be a positive integer")
    chart = common.WORKTREES_DIR / f"inc-{incident_id}" / "helm" / "ping-devops"
    if not chart.is_dir():
        raise ValueError(f"no worktree for incident {incident_id}; run a repo_config patch tool first")

    branch = _helm("template", RELEASE, str(chart), "--namespace", NAMESPACE)
    if branch.returncode != 0:
        return {"valid": False, "errors": [branch.stderr.strip()[:1000]], "patch_changed_lines": 0,
                "rendered_diff": "", "deployed_differs_from_main_lines": None}

    with tempfile.TemporaryDirectory() as tmp:
        archive = subprocess.run(["git", "archive", BASE_REF, "helm/ping-devops"], cwd=common.REPO_ROOT, capture_output=True)
        if archive.returncode != 0:
            raise ValueError(f"cannot read {BASE_REF}")
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)
        base = _helm("template", RELEASE, f"{tmp}/helm/ping-devops", "--namespace", NAMESPACE)
    if base.returncode != 0:
        raise RuntimeError(f"{BASE_REF} itself does not render: {base.stderr.strip()[:500]}")

    changed, diff = _diff(base.stdout, branch.stdout, BASE_REF, f"remediation/inc-{incident_id}")

    deployed = _helm("get", "manifest", RELEASE, "--namespace", NAMESPACE, "--kube-context", KUBE_CONTEXT)
    drift = _diff(deployed.stdout, base.stdout, "deployed", BASE_REF)[0] if deployed.returncode == 0 else None

    return {"valid": True, "errors": [], "patch_changed_lines": changed,
            "rendered_diff": diff[:MAX_DIFF_CHARS], "deployed_differs_from_main_lines": drift}



# --- deployed release history and values ---

import json
import re

from mcp_servers.common import is_protected_key

_KEY_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,200}$")


def lookup_path(obj, path: str):
    """Walk a dotted key path through nested dicts; None if any step is missing."""
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def _mask(path: str, value):
    return "***" if is_protected_key(path.split(".")[-1]) else value


@mcp.tool()
def get_helm_release(key_paths: list[str] | None = None, revisions: list[int] | None = None) -> dict:
    """The deployed Helm release: its revision history (revision, time,
    status, chart, description) and, for the key_paths you ask about, the
    value each one had in the given revisions (default: the current one).
    key_paths are dotted paths into the chart values, e.g.
    'pingfederate-engine.image.tag'. Compare an earlier healthy revision with
    the current one to see what changed (a bad image tag, a changed limit),
    and compare with the repo to tell cluster-only drift from a bad commit.
    At most 10 key paths and 3 revisions; secret-looking keys are masked.
    Returns {history: [...], values: {revision: {key_path: value}}}."""
    key_paths = key_paths or []
    if len(key_paths) > 10 or not all(_KEY_PATH_RE.match(k) for k in key_paths):
        raise ValueError("key_paths: at most 10, each a dotted path of letters, digits, '_', '-', '.'")
    if revisions and (len(revisions) > 3 or any((not isinstance(r, int)) or r <= 0 for r in revisions)):
        raise ValueError("revisions: at most 3 positive integers")

    hist = _helm("history", RELEASE, "--namespace", NAMESPACE, "--kube-context", KUBE_CONTEXT, "--max", "10", "-o", "json")
    if hist.returncode != 0:
        raise ValueError(f"helm history failed: {hist.stderr.strip()[:300]}")
    history = [{k: h.get(k) for k in ("revision", "updated", "status", "chart", "app_version", "description")}
               for h in json.loads(hist.stdout)]

    values = {}
    if key_paths:
        for rev in (revisions or [history[-1]["revision"]]):
            got = _helm("get", "values", RELEASE, "--all", "--namespace", NAMESPACE, "--kube-context", KUBE_CONTEXT,
                        "--revision", str(rev), "-o", "json")
            if got.returncode != 0:
                raise ValueError(f"helm get values (revision {rev}) failed: {got.stderr.strip()[:300]}")
            doc = json.loads(got.stdout)
            values[str(rev)] = {k: _mask(k, lookup_path(doc, k)) for k in key_paths}
    return {"history": history, "values": values}


if __name__ == "__main__":
    mcp.run()
