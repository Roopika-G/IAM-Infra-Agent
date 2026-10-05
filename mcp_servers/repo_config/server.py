"""repo_config MCP server: the ONLY write-capable server in the agent's
toolset, and it can only write to a local git worktree on a branch named
remediation/inc-<id>. It never pushes and never touches main or the live
cluster -- opening the PR is a separate step (GitHub MCP, Phase 10), and
deploying is CI/CD's (helm_deploy, Phase 12).

  patch_helm_values     -- change one scalar in helm/ping-devops/values.yaml
  patch_server_profile  -- change one key in a .properties(.subst) file under helm/server-profile/

Safety, applied to both tools:
  * the file's current sha256 must be passed back (expected_file_sha256) --
    refuses if the file changed since the agent read it
  * only the two allowed locations are reachable (no traversal)
  * keys that look like secrets/licenses/keys are refused outright
  * exactly the target value changes; comments and formatting are preserved
  * the result is committed locally on the incident branch and a diff returned

Run: uv run python mcp_servers/repo_config/server.py
"""

import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mcp_servers import common  # noqa: E402

BASE_REF = "main"
AGENT_IDENTITY = ["-c", "user.name=self-healing-iam-agent", "-c", "user.email=agent@self-healing-iam.local"]
MAX_DIFF_CHARS = 20000

mcp = MCPServer("repo_config")


def _incident_worktree(incident_id: int) -> tuple[Path, str]:
    if not isinstance(incident_id, int) or incident_id <= 0:
        raise ValueError("incident_id must be a positive integer")
    branch = f"remediation/inc-{incident_id}"
    path = common.WORKTREES_DIR / f"inc-{incident_id}"
    if not path.exists():
        common.WORKTREES_DIR.mkdir(exist_ok=True)
        common.git("worktree", "add", "-b", branch, str(path), BASE_REF)
    return path, branch


def _apply(incident_id: int, rel_path: str, expected_sha: str, patcher, description: str) -> dict:
    worktree, branch = _incident_worktree(incident_id)
    file_path = worktree / rel_path
    text = file_path.read_text()

    if common.sha256_text(text) != expected_sha:
        raise ValueError(
            f"{rel_path} changed since it was read (sha256 mismatch); re-read it and retry"
        )

    patched = patcher(text)
    file_path.write_text(patched)

    common.git("add", rel_path, cwd=worktree)
    common.git(*AGENT_IDENTITY, "commit", "-m", f"remediation inc-{incident_id}: {description}", cwd=worktree)

    base = common.git("merge-base", BASE_REF, "HEAD", cwd=worktree).strip()
    diff = common.git("diff", base, "HEAD", cwd=worktree)
    return {
        "branch": branch,
        "worktree": str(worktree),
        "commit": common.git("rev-parse", "HEAD", cwd=worktree).strip(),
        "unified_diff": diff[:MAX_DIFF_CHARS],
        "new_file_sha256": common.sha256_text(patched),
        "pushed": False,
    }


@mcp.tool()
def patch_helm_values(key_path: str, new_value: str, expected_file_sha256: str, incident_id: int) -> dict:
    """Change one scalar in helm/ping-devops/values.yaml on the local branch
    remediation/inc-<incident_id> (created from main if new). key_path is
    dotted, e.g. 'pingfederate-admin.envs.POSTGRES_JDBC_URL'.
    expected_file_sha256 is the file_sha256 from repo_read's
    read_values_yaml_key (or new_file_sha256 from a previous patch on the
    same branch). Commits locally, never pushes. Returns {branch, worktree,
    commit, unified_diff, new_file_sha256, pushed: false}."""
    common.validate_new_value(new_value)
    if common.is_protected_key(key_path.split(".")[-1]):
        raise ValueError(f"{key_path!r} looks like a secret/license/key; never auto-patched, escalate to a human")
    return _apply(
        incident_id,
        common.VALUES_FILE,
        expected_file_sha256,
        lambda text: common.patch_yaml_scalar(text, key_path, new_value),
        f"set {key_path}",
    )


@mcp.tool()
def patch_server_profile(path: str, key: str, new_value: str, expected_file_sha256: str, incident_id: int) -> dict:
    """Change one key=value line in a .properties or .properties.subst file
    under helm/server-profile/ on the local branch remediation/inc-<id>.
    Only properties-style files are supported (JSON/XML files are refused).
    expected_file_sha256 is the file_sha256 from read_server_profile_file.
    Commits locally, never pushes. Returns {branch, worktree, commit,
    unified_diff, new_file_sha256, pushed: false}."""
    path = common.validate_repo_path(path, allow_values=False)
    if not path.endswith(common.PROPERTIES_SUFFIXES):
        raise ValueError(f"only {common.PROPERTIES_SUFFIXES} files can be patched, got {path!r}")
    common.validate_new_value(new_value)
    if common.is_protected_key(key):
        raise ValueError(f"{key!r} looks like a secret/license/key; never auto-patched, escalate to a human")
    return _apply(
        incident_id,
        path,
        expected_file_sha256,
        lambda text: common.patch_properties_key(text, key, new_value),
        f"set {key} in {path}",
    )


if __name__ == "__main__":
    mcp.run()
