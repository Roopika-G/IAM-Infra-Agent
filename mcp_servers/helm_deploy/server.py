"""helm_deploy MCP server: the write-capable half of Helm. Point CI/CD at
this server -- NOT the diagnosis agent (plan.md Phase 9/12).

  helm_upgrade_release   -- deploy the chart at one exact git commit
  helm_rollback_release  -- roll back to a previous revision

Interim authorization: every call must present a token equal to the
HELM_DEPLOY_TOKEN environment variable, and the server refuses to do
anything at all if that variable is unset. This is a stand-in for Phase 11's
single-use, commit-bound, SPIFFE-verified tokens -- it gates the tools, it
does not yet bind a token to one commit or one use.

Run: HELM_DEPLOY_TOKEN=... uv run python mcp_servers/helm_deploy/server.py
"""

import hmac
import os
import re
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
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_TIMEOUT_RE = re.compile(r"^\d{1,4}[sm]$")

mcp = MCPServer("helm_deploy")


def _check_token(token: str) -> None:
    expected = os.environ.get("HELM_DEPLOY_TOKEN")
    if not expected:
        raise PermissionError("HELM_DEPLOY_TOKEN is not set on this server; refusing all deploy actions")
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise PermissionError("invalid deploy token")


def _run(*cmd: str) -> str:
    result = subprocess.run(list(cmd), capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} {cmd[1]} failed: {result.stderr.strip()[:800]}")
    return result.stdout


def _status() -> dict:
    import json
    out = _run("helm", "status", RELEASE, "--namespace", NAMESPACE, "--kube-context", KUBE_CONTEXT, "-o", "json")
    info = json.loads(out)
    return {"revision": info["version"], "status": info["info"]["status"]}


@mcp.tool()
def helm_upgrade_release(commit_sha: str, token: str, timeout: str = "10m", restart_pods: bool = True) -> dict:
    """Deploy helm/ping-devops exactly as it exists at commit_sha with
    `helm upgrade --install --atomic --wait` (a failed rollout rolls itself
    back). restart_pods also does a rollout restart afterwards, because the
    server profile is git-cloned at pod start and a profile-only change would
    otherwise not take effect. Returns {revision, status, commit}."""
    _check_token(token)
    if not _SHA_RE.match(commit_sha):
        raise ValueError("commit_sha must be 7-40 hex characters")
    if not _TIMEOUT_RE.match(timeout):
        raise ValueError("timeout must look like '10m' or '90s'")

    with tempfile.TemporaryDirectory() as tmp:
        archive = subprocess.run(
            ["git", "archive", commit_sha, "helm/ping-devops"], cwd=common.REPO_ROOT, capture_output=True
        )
        if archive.returncode != 0:
            raise ValueError(f"unknown commit: {commit_sha}")
        subprocess.run(["tar", "-x", "-C", tmp], input=archive.stdout, check=True)

        _run("helm", "upgrade", "--install", RELEASE, f"{tmp}/helm/ping-devops",
             "--namespace", NAMESPACE, "--kube-context", KUBE_CONTEXT,
             "--atomic", "--wait", "--timeout", timeout)

    if restart_pods:
        _run("kubectl", "--context", KUBE_CONTEXT, "rollout", "restart", "deployment",
             "--namespace", NAMESPACE, "-l", f"app.kubernetes.io/instance={RELEASE}")

    return {**_status(), "commit": commit_sha}


@mcp.tool()
def helm_rollback_release(revision: int, token: str, timeout: str = "10m") -> dict:
    """Roll the release back to a previous Helm revision (`helm rollback
    --wait`). Returns {revision, status} of the release afterwards."""
    _check_token(token)
    if not isinstance(revision, int) or revision <= 0:
        raise ValueError("revision must be a positive integer")
    if not _TIMEOUT_RE.match(timeout):
        raise ValueError("timeout must look like '10m' or '90s'")
    _run("helm", "rollback", RELEASE, str(revision), "--namespace", NAMESPACE,
         "--kube-context", KUBE_CONTEXT, "--wait", "--timeout", timeout)
    return _status()


if __name__ == "__main__":
    mcp.run()
