"""kubernetes MCP server: read-only view of the pingfederate namespace.

Three tools, one server -- all three talk to the same k8s API with the
same credential:

  get_pod_status         -- "is the pod Ready, restarting, or crashing?"
  get_events             -- "did Kubernetes itself report anything?"
  get_live_config_value  -- "what is this pod actually running right now?"

Read-only by construction: the namespace is a constant (not a tool
parameter), and the one non-GET call (an exec) runs a fixed `printenv`
against a key validated as a plain env var name -- never caller-supplied
shell text.

Run (stdio transport, how an MCP client launches it):
    uv run python mcp_servers/kubernetes/server.py
"""

import os
import re
import threading

from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.stream import stream
from mcp.server.mcpserver import MCPServer

KUBE_CONTEXT = os.environ.get("KUBE_CONTEXT", "kind-self-healing-iam")
NAMESPACE = "pingfederate"

MAX_EVENTS = 50
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

mcp = MCPServer("kubernetes")

_core: client.CoreV1Api | None = None
# kubernetes.stream.stream() temporarily swaps a method on the shared ApiClient,
# so concurrent execs on it corrupt each other -- serialize them.
_exec_lock = threading.Lock()


def _api() -> client.CoreV1Api:
    global _core
    if _core is None:
        config.load_kube_config(context=KUBE_CONTEXT)
        _core = client.CoreV1Api()
    return _core


def _container_summary(cs) -> dict:
    state = cs.state
    if state.running:
        current = {"state": "running", "since": state.running.started_at.isoformat()}
    elif state.waiting:
        current = {"state": "waiting", "reason": state.waiting.reason, "message": state.waiting.message}
    else:
        current = {"state": "terminated", "reason": state.terminated.reason, "exit_code": state.terminated.exit_code}

    last = None
    if cs.last_state and cs.last_state.terminated:
        t = cs.last_state.terminated
        last = {"reason": t.reason, "exit_code": t.exit_code, "finished_at": t.finished_at.isoformat() if t.finished_at else None}

    return {
        "name": cs.name,
        "ready": cs.ready,
        "restart_count": cs.restart_count,
        "current": current,
        "last_terminated": last,
    }


def _pod_summary(pod) -> dict:
    statuses = pod.status.container_statuses or []
    ready = sum(1 for cs in statuses if cs.ready)
    return {
        "pod": pod.metadata.name,
        "phase": pod.status.phase,
        "ready": f"{ready}/{len(statuses)}",
        "containers": [_container_summary(cs) for cs in statuses],
    }


@mcp.tool()
def get_pod_status(pod: str | None = None) -> list[dict]:
    """Status of pods in the pingfederate namespace. With no pod name,
    returns every pod (use this first to discover names). Each entry is
    {pod, phase, ready: '2/2', containers: [{name, ready, restart_count,
    current: {state, reason?}, last_terminated: {reason, exit_code}?}]}.
    Note a pod can be 'Running 2/2' while the application inside it is
    still failing -- check logs/config, not just this."""
    api = _api()
    if pod:
        pods = [api.read_namespaced_pod(pod, NAMESPACE)]
    else:
        pods = api.list_namespaced_pod(NAMESPACE).items
    return [_pod_summary(p) for p in pods]


@mcp.tool()
def get_events(pod: str | None = None, limit: int = 20) -> list[dict]:
    """Kubernetes events in the pingfederate namespace, newest first
    (e.g. image pull failures, probe failures, restarts). Optionally filter
    to one pod by name. Returns at most 50 as {time, type, reason, object,
    message}. An empty list means Kubernetes reported nothing."""
    limit = max(1, min(limit, MAX_EVENTS))
    kwargs = {}
    if pod:
        kwargs["field_selector"] = f"involvedObject.name={pod}"
    events = _api().list_namespaced_event(NAMESPACE, **kwargs).items
    events.sort(key=lambda e: e.last_timestamp or e.event_time or e.metadata.creation_timestamp, reverse=True)
    return [
        {
            "time": (e.last_timestamp or e.event_time or e.metadata.creation_timestamp).isoformat(),
            "type": e.type,
            "reason": e.reason,
            "object": f"{e.involved_object.kind}/{e.involved_object.name}",
            "message": e.message,
        }
        for e in events[:limit]
    ]


@mcp.tool()
def get_live_config_value(pod: str, key: str, container: str | None = None) -> dict:
    """The value of one environment variable as the running container
    actually sees it (not what a ConfigMap or values.yaml says -- an env
    var is fixed at container start). key is the bare variable name, e.g.
    'POSTGRES_JDBC_URL'. container defaults to the pod's first container.
    Returns {pod, container, key, value}; value is null if the variable
    isn't set."""
    if not _ENV_NAME_RE.match(key):
        raise ValueError(f"key must be a plain environment variable name, got {key!r}")

    api = _api()
    if container is None:
        container = api.read_namespaced_pod(pod, NAMESPACE).spec.containers[0].name

    with _exec_lock:
        out = stream(
            api.connect_get_namespaced_pod_exec,
            pod,
            NAMESPACE,
            container=container,
            command=["printenv", key],
            stderr=False,
            stdin=False,
            stdout=True,
            tty=False,
        )
    value = out.rstrip("\n") if out else None
    return {"pod": pod, "container": container, "key": key, "value": value or None}



# --- additional read tools: logs, pod spec, workload status, secret metadata ---

MAX_LOG_LINES = 500
MAX_LOG_LINE_CHARS = 500
_LOG_SECRET_RE = re.compile(r"(?i)(password|passwd|secret|token|authorization|api[_-]?key)(\s*[=:]\s*|\"\s*:\s*\")([^\s\",]+)")
_RESOURCE_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")

_apps: client.AppsV1Api | None = None


def _apps_api() -> client.AppsV1Api:
    global _apps
    if _apps is None:
        _api()  # loads kube config
        _apps = client.AppsV1Api()
    return _apps


def _api_error(e: ApiException) -> ValueError:
    return ValueError(f"Kubernetes API: {e.reason} ({e.status})")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _redact_log_line(line: str) -> str:
    line = _ANSI_RE.sub("", line)  # terminal colour codes add noise, not information
    return _LOG_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", line)[:MAX_LOG_LINE_CHARS]


def _probe(p) -> dict | None:
    if p is None:
        return None
    kind = "http" if p.http_get else "tcp" if p.tcp_socket else "exec" if p._exec else "other"
    return {
        "type": kind,
        "path": p.http_get.path if p.http_get else None,
        "port": str((p.http_get or p.tcp_socket).port) if (p.http_get or p.tcp_socket) else None,
        "initial_delay_seconds": p.initial_delay_seconds,
        "period_seconds": p.period_seconds,
        "timeout_seconds": p.timeout_seconds,
        "failure_threshold": p.failure_threshold,
    }


def _format_pod_spec(pod) -> dict:
    owner = (pod.metadata.owner_references or [None])[0]
    containers = []
    for c in pod.spec.containers:
        env = []
        for e in c.env or []:
            vf = e.value_from
            source = "literal" if vf is None else "secret" if vf.secret_key_ref else "configMap" if vf.config_map_key_ref else "other"
            env.append({"name": e.name, "source": source})  # values are deliberately never included
        containers.append({
            "name": c.name,
            "image": c.image,
            "image_pull_policy": c.image_pull_policy,
            "resources": {"requests": (c.resources.requests if c.resources else None), "limits": (c.resources.limits if c.resources else None)},
            "readiness_probe": _probe(c.readiness_probe),
            "liveness_probe": _probe(c.liveness_probe),
            "startup_probe": _probe(c.startup_probe),
            "env": env,
            "env_from": [(r.config_map_ref.name if r.config_map_ref else "secret:" + r.secret_ref.name) for r in (c.env_from or [])],
        })
    return {
        "pod": pod.metadata.name,
        "owner": f"{owner.kind}/{owner.name}" if owner else None,
        "restart_policy": pod.spec.restart_policy,
        "containers": containers,
    }


def _format_workload(kind: str, o) -> dict:
    st = o.status
    strategy = (o.spec.strategy.type if kind == "deployment" and o.spec.strategy else
                o.spec.update_strategy.type if kind == "statefulset" and o.spec.update_strategy else None)
    return {
        "kind": kind,
        "name": o.metadata.name,
        "desired": o.spec.replicas,
        "ready": getattr(st, "ready_replicas", None) or 0,
        "updated": getattr(st, "updated_replicas", None) or 0,
        "available": getattr(st, "available_replicas", None) or 0,
        "strategy": strategy,
        "images": [c.image for c in o.spec.template.spec.containers],
        "conditions": [{"type": c.type, "status": c.status, "reason": c.reason, "message": (c.message or "")[:200]}
                       for c in (getattr(st, "conditions", None) or [])],
    }


def _secret_summary(sec) -> dict:
    """Names, sizes and timestamps only. The secret's values are never read out."""
    times = [m.time for m in (sec.metadata.managed_fields or []) if m.time]
    return {
        "name": sec.metadata.name,
        "type": sec.type,
        "keys": [{"key": k, "size_bytes": len(v or "") * 3 // 4} for k, v in sorted((sec.data or {}).items())],
        "created": sec.metadata.creation_timestamp.isoformat() if sec.metadata.creation_timestamp else None,
        "last_modified": max(times).isoformat() if times else None,
    }


@mcp.tool()
def get_pod_logs(pod: str, container: str | None = None, previous: bool = False, tail_lines: int = 100, contains: str | None = None) -> dict:
    """Recent log lines straight from a container (what the container itself
    printed), unlike get_logs which reads PingFederate's shipped log table.
    Use previous=true for the last terminated container of a crash-looping
    pod. container defaults to the pod's first container. contains is a
    case-insensitive filter. At most 500 lines, each cut at 500 characters,
    and anything that looks like password/secret/token=value is masked.
    Returns {pod, container, previous, returned, lines}."""
    tail_lines = max(1, min(tail_lines, MAX_LOG_LINES))
    api = _api()
    try:
        if container is None:
            container = api.read_namespaced_pod(pod, NAMESPACE).spec.containers[0].name
        # Read raw bytes and decode ourselves: the client's own str conversion
        # returned a bytes repr for some containers' previous logs.
        resp = api.read_namespaced_pod_log(pod, NAMESPACE, container=container, previous=previous,
                                           tail_lines=tail_lines, _request_timeout=30, _preload_content=False)
        text = resp.data.decode("utf-8", "replace")
    except ApiException as e:
        raise _api_error(e)
    lines = text.splitlines()
    if contains:
        lines = [l for l in lines if contains.lower() in l.lower()]
    lines = [_redact_log_line(l) for l in lines][-tail_lines:]
    return {"pod": pod, "container": container, "previous": previous, "returned": len(lines), "lines": lines}


@mcp.tool()
def get_pod_spec(pod: str) -> dict:
    """What a pod is configured to run, as Kubernetes holds it: per
    container the image (and tag), pull policy, resource requests/limits,
    readiness/liveness/startup probes, and environment variable NAMES with
    where each comes from (literal / configMap / secret). Values are never
    included (use get_live_config_value for a specific env var). Use it for
    image-tag, memory-limit and probe faults."""
    try:
        return _format_pod_spec(_api().read_namespaced_pod(pod, NAMESPACE))
    except ApiException as e:
        raise _api_error(e)


@mcp.tool()
def get_workload_status(name: str | None = None) -> list[dict]:
    """Desired vs ready replicas for Deployments and StatefulSets in the
    pingfederate namespace (all of them, or just the one called name). Shows
    what get_pod_status cannot: a workload scaled to 0 has no pod to list.
    Each entry: {kind, name, desired, ready, updated, available, strategy,
    images, conditions}."""
    apps = _apps_api()
    try:
        items = [("deployment", o) for o in apps.list_namespaced_deployment(NAMESPACE).items] + \
                [("statefulset", o) for o in apps.list_namespaced_stateful_set(NAMESPACE).items]
    except ApiException as e:
        raise _api_error(e)
    out = [_format_workload(k, o) for k, o in items if name is None or o.metadata.name == name]
    if name and not out:
        raise ValueError(f"no deployment or statefulset named {name!r}")
    return out


@mcp.tool()
def get_secret_keys(name: str) -> dict:
    """Metadata about a Kubernetes Secret in the pingfederate namespace:
    which keys it holds, their sizes, when it was created and last modified.
    NEVER returns the secret values. Use it to check a secret exists and
    whether it changed recently (e.g. credential faults). Helm release
    secrets are refused. Returns {name, type, keys: [{key, size_bytes}],
    created, last_modified}."""
    if not _RESOURCE_NAME_RE.match(name) or len(name) > 253:
        raise ValueError(f"invalid secret name: {name!r}")
    if name.startswith("sh.helm.release."):
        raise ValueError("Helm release secrets are not readable through this tool")
    try:
        return _secret_summary(_api().read_namespaced_secret(name, NAMESPACE))
    except ApiException as e:
        raise _api_error(e)


if __name__ == "__main__":
    mcp.run()
