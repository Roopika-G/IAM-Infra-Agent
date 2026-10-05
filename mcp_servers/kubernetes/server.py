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


if __name__ == "__main__":
    mcp.run()
