"""Shared, pure-logic helpers for the repo_read / repo_config / helm_* MCP
servers: path and ref validation, git subprocess wrapper, comment-preserving
YAML scalar patching, and properties-file patching.

Kept free of MCP imports so it can be unit-tested directly
(tests/test_mcp_common.py). Nothing here shells out through a shell string --
git is always invoked with an argument list.
"""

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKTREES_DIR = REPO_ROOT / ".worktrees"

VALUES_FILE = "helm/ping-devops/values.yaml"
SERVER_PROFILE_PREFIX = "helm/server-profile/"
PROPERTIES_SUFFIXES = (".properties", ".properties.subst")

_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")
_PROTECTED_KEY_RE = re.compile(r"PASSWORD|SECRET|LICENSE|JWK|PRIVATE|TOKEN|PASSPHRASE", re.IGNORECASE)
_PLAIN_SAFE_RE = re.compile(r"^[A-Za-z0-9_./@-]+$")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def validate_ref(ref: str) -> str:
    """A git ref/branch name: no leading '-', no spaces or shell/rev syntax."""
    if not _REF_RE.match(ref) or ".." in ref:
        raise ValueError(f"invalid git ref: {ref!r}")
    return ref


def validate_repo_path(path: str, *, allow_values: bool = True) -> str:
    """Only helm/ping-devops/values.yaml and files under helm/server-profile/
    are reachable. Rejects absolute paths, '..' segments, and anything else."""
    if not path or "\x00" in path or path.startswith("/"):
        raise ValueError(f"path must be a relative repo path, got {path!r}")
    parts = PurePosixPath(path).parts
    if ".." in parts or "." in parts:
        raise ValueError(f"path traversal not allowed: {path!r}")
    norm = str(PurePosixPath(*parts))
    if allow_values and norm == VALUES_FILE:
        return norm
    if norm.startswith(SERVER_PROFILE_PREFIX):
        return norm
    raise ValueError(f"path outside the allowed set ({VALUES_FILE}, {SERVER_PROFILE_PREFIX}**): {path!r}")


def is_protected_key(key: str) -> bool:
    """Secrets/license/jwk keys must never be auto-patched (see
    knowledge/golden-architecture.md, 'Never auto-patch these')."""
    return bool(_PROTECTED_KEY_RE.search(key))


def validate_new_value(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1000:
        raise ValueError("new_value must be a non-empty string up to 1000 chars")
    if any(ord(c) < 32 for c in value):
        raise ValueError("new_value must not contain control characters or newlines")
    return value


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd or REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


def git_show(ref: str, path: str) -> str:
    validate_ref(ref)
    return git("show", f"{ref}:{path}")


# --- YAML: find and patch one scalar, preserving comments and formatting ---

def find_scalar(text: str, key_path: str) -> yaml.ScalarNode:
    """Walk a dotted key path (e.g. 'pingfederate-admin.envs.POSTGRES_JDBC_URL')
    through the YAML node tree and return the scalar node at the end."""
    node = yaml.compose(text)
    if node is None:
        raise ValueError("file is empty")
    walked = []
    for part in key_path.split("."):
        walked.append(part)
        if not isinstance(node, yaml.MappingNode):
            raise ValueError(f"{'.'.join(walked[:-1])!r} is not a mapping; cannot descend into {part!r}")
        for k, v in node.value:
            if k.value == part:
                node = v
                break
        else:
            raise ValueError(f"key not found: {'.'.join(walked)!r}")
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError(f"{key_path!r} is not a scalar value")
    return node


def render_scalar(node: yaml.ScalarNode, new_value: str) -> str:
    """Emit new_value in the same quoting style as the node it replaces."""
    if node.style == "'":
        return "'" + new_value.replace("'", "''") + "'"
    if node.style == '"' or not _PLAIN_SAFE_RE.match(new_value):
        escaped = new_value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return new_value


def patch_yaml_scalar(text: str, key_path: str, new_value: str) -> str:
    node = find_scalar(text, key_path)
    if node.value == new_value:
        raise ValueError(f"{key_path!r} already has that value; nothing to change")
    start, end = node.start_mark.index, node.end_mark.index
    patched = text[:start] + render_scalar(node, new_value) + text[end:]

    check = find_scalar(patched, key_path)
    if check.value != new_value:
        raise ValueError("patched file did not read back with the requested value; refusing")
    return patched


def yaml_path_at_line(text: str, line: int) -> str | None:
    """Dotted key path of the YAML key/value that sits on a 1-based line, or
    None (comment, blank, or unparseable). Lets a text search hit be turned
    into the exact path the patch tools take."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:
        return None

    def walk(node, prefix):
        if isinstance(node, yaml.MappingNode):
            for k, v in node.value:
                path = prefix + [str(k.value)]
                if isinstance(v, yaml.ScalarNode):
                    if k.start_mark.line + 1 == line or v.start_mark.line + 1 <= line <= v.end_mark.line + 1:
                        return ".".join(path)
                else:
                    found = walk(v, path)
                    if found:
                        return found
                    if k.start_mark.line + 1 == line:
                        return ".".join(path)
        elif isinstance(node, yaml.SequenceNode):
            for i, item in enumerate(node.value):
                found = walk(item, prefix + [str(i)])
                if found:
                    return found
        return None

    return walk(root, []) if root is not None else None


# --- properties files: key=value lines ---

def patch_properties_key(text: str, key: str, new_value: str) -> str:
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*)$", re.MULTILINE)
    matches = pattern.findall(text)
    if not matches:
        raise ValueError(f"key not found: {key!r}")
    if len(matches) > 1:
        raise ValueError(f"key appears {len(matches)} times; refusing to guess which to patch: {key!r}")
    if matches[0][1] == new_value:
        raise ValueError(f"{key!r} already has that value; nothing to change")
    return pattern.sub(lambda m: m.group(1) + new_value, text, count=1)
