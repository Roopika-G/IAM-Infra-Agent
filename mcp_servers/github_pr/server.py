"""github_pr MCP server: the agent's only way to publish a remediation --
push its incident branch and open a pull request. Built on the already-
authenticated `gh` CLI rather than the prebuilt GitHub MCP server, so the
exposed surface is exactly three narrow tools and nothing else.

  push_remediation_branch  -- push remediation/inc-<id> (never force, never any other ref)
  create_pull_request      -- open a PR from that branch into main
  add_pr_comment           -- comment on a remediation PR (e.g. the helm dry-run output)
  create_github_issue      -- open an issue to hand a problem to a human (escalation)

Deliberately NOT here: merge, approve, close, review, or any repo setting. (create_github_issue only ever opens a new issue.)
A human merges; branch protection on main (Phase 10) enforces it. These
tools can only touch branches named remediation/inc-<number> and PRs whose
head is such a branch.

Run: uv run python mcp_servers/github_pr/server.py
"""

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mcp_servers import common  # noqa: E402

REMOTE = "github"
BASE_BRANCH = "main"
MAX_TITLE = 200
MAX_BODY = 20000
_BRANCH_RE = re.compile(r"^remediation/inc-[1-9][0-9]{0,9}$")

mcp = MCPServer("github_pr")


def _branch(incident_id: int) -> str:
    if not isinstance(incident_id, int) or incident_id <= 0:
        raise ValueError("incident_id must be a positive integer")
    branch = f"remediation/inc-{incident_id}"
    assert _BRANCH_RE.match(branch)
    return branch


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], cwd=common.REPO_ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"gh {args[0]} {args[1]} failed: {result.stderr.strip()[:500]}")
    return result.stdout.strip()


def _validate_text(title: str | None, body: str) -> None:
    if title is not None and (not title.strip() or len(title) > MAX_TITLE or "\n" in title):
        raise ValueError(f"title must be one non-empty line up to {MAX_TITLE} chars")
    if not body.strip() or len(body) > MAX_BODY:
        raise ValueError(f"body must be non-empty and up to {MAX_BODY} chars")


@mcp.tool()
@common.visible_errors
def push_remediation_branch(incident_id: int) -> dict:
    """Push the local branch remediation/inc-<incident_id> (created by
    repo_config) to GitHub. Only that one branch, never forced. Returns
    {branch, remote, commit}."""
    branch = _branch(incident_id)
    commit = common.git("rev-parse", "--verify", f"refs/heads/{branch}").strip()
    common.git("push", REMOTE, f"refs/heads/{branch}:refs/heads/{branch}")
    return {"branch": branch, "remote": REMOTE, "commit": commit}


@mcp.tool()
@common.visible_errors
def create_pull_request(incident_id: int, title: str, body: str) -> dict:
    """Open a pull request from remediation/inc-<incident_id> into main. The
    branch must already be pushed (push_remediation_branch). body should
    carry the diagnosis, the evidence tool calls, and the diff summary --
    that is what the human reviewer reads. Returns {branch, url, number}."""
    branch = _branch(incident_id)
    _validate_text(title, body)
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(body)
        f.flush()
        url = _gh("pr", "create", "--base", BASE_BRANCH, "--head", branch, "--title", title, "--body-file", f.name)
    return {"branch": branch, "url": url, "number": int(url.rstrip("/").rsplit("/", 1)[-1])}


@mcp.tool()
@common.visible_errors
def add_pr_comment(pr_number: int, body: str) -> dict:
    """Comment on a remediation pull request (one whose head branch is
    remediation/inc-<n>). Refuses any other PR. Returns {pr_number, url}."""
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    _validate_text(None, body)
    head = json.loads(_gh("pr", "view", str(pr_number), "--json", "headRefName"))["headRefName"]
    if not _BRANCH_RE.match(head):
        raise PermissionError(f"PR #{pr_number} is not a remediation PR (head is {head!r}); refusing to comment")
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(body)
        f.flush()
        url = _gh("pr", "comment", str(pr_number), "--body-file", f.name)
    return {"pr_number": pr_number, "url": url}


@mcp.tool()
@common.visible_errors
def create_github_issue(title: str, body: str, incident_id: int | None = None) -> dict:
    """Open a GitHub issue to hand a problem to a human: the escalation path
    when the agent cannot or must not fix something itself (a secret, a
    license, a policy refusal, an unclear cause). body should be the full
    report: what happened, what was checked, what was found, what was ruled
    out, and what a human must do. incident_id, if given, is prefixed to the
    title. Returns {url, number}. Creates an issue only; it cannot edit or
    close anything."""
    if incident_id is not None and (not isinstance(incident_id, int) or incident_id <= 0):
        raise ValueError("incident_id must be a positive integer")
    _validate_text(title, body)
    full_title = f"[incident #{incident_id}] {title}" if incident_id else title
    with tempfile.NamedTemporaryFile("w", suffix=".md") as f:
        f.write(body)
        f.flush()
        url = _gh("issue", "create", "--title", full_title, "--body-file", f.name)
    return {"url": url, "number": int(url.rstrip("/").rsplit("/", 1)[-1])}


if __name__ == "__main__":
    mcp.run()
