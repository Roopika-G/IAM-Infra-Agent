"""Git housekeeping the agent does before each attempt.

Every attempt starts from what is on GitHub right now, on a fresh local branch.
That matters for a retry: the previous attempt's branch was already merged (or
left behind), and building on top of it would mix attempts together.
"""

from pathlib import Path

from mcp_servers import common


def prepare_attempt(incident_id: int, root: Path = common.REPO_ROOT) -> None:
    """Fetch GitHub's main and discard any leftover worktree and branch for this incident."""
    common.git("fetch", "github", "main", cwd=root)
    path = root / ".worktrees" / f"inc-{incident_id}"
    branch = f"remediation/inc-{incident_id}"
    if path.exists():
        common.git("worktree", "remove", "--force", str(path), cwd=root)
    common.git("worktree", "prune", cwd=root)
    if branch in common.git("branch", "--list", branch, cwd=root):
        common.git("branch", "-D", branch, cwd=root)
