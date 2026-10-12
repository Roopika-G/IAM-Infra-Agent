"""Intake: take a snapshot of the system before the LLM starts.

Plain code, no LLM. It calls read tools directly so the agent begins with the
big picture (are pods up? did any config drift? how long do the license and
certificates have left?) instead of discovering it one call at a time.

Nothing here may crash the run: if a source is unreachable it is recorded as
unavailable and the agent is told, which is itself useful evidence.
"""

from agent.schemas import DriftRow, Evidence, Incident, PodRow, Snapshot
from agent.toolbox import ToolFailed


def component_of(pod_name: str) -> str | None:
    if "admin" in pod_name:
        return "admin"
    if "engine" in pod_name:
        return "engine"
    if pod_name.startswith("postgres"):
        return "postgres"
    return None


def drift_state(golden: str, live: str | None) -> str:
    return "unknown" if live is None else ("ok" if live == golden else "drift")


async def _try(tools, name: str, args: dict, unavailable: list[str]):
    try:
        return await tools.call(name, args)
    except ToolFailed as e:
        unavailable.append(f"{name} ({str(e)[:80]})")
        return None


async def scan_drift(tools, unavailable: list[str]) -> tuple[list[PodRow], list[DriftRow]]:
    """The pods, and every baseline key against what its running container actually sees."""
    pod_rows, by_component = [], {}
    for p in await _try(tools, "get_pod_status", {}, unavailable) or []:
        pod_rows.append(PodRow(name=p["pod"], phase=p["phase"], ready=p["ready"], restarts=sum(c["restart_count"] for c in p["containers"])))
        if component_of(p["pod"]):
            by_component[component_of(p["pod"])] = p["pod"]
    drift_rows = []
    for row in await _try(tools, "list_baseline", {}, unavailable) or []:
        product, _, env_name = row["key"].partition(".envs.")
        pod = by_component.get(product.removeprefix("pingfederate-"))
        live = None
        if pod and env_name:
            got = await _try(tools, "get_live_config_value", {"pod": pod, "key": env_name}, unavailable)
            live = got["value"] if got else None
        drift_rows.append(DriftRow(key=row["key"], golden=row["golden_value"], live=live, state=drift_state(row["golden_value"], live)))
    return pod_rows, drift_rows


async def build_snapshot(tools, incident: Incident, evidence_chars: int = 400) -> tuple[Snapshot, list[Evidence]]:
    snap = Snapshot()
    evidence: list[Evidence] = []

    def note(tool: str, args: dict, summary: str) -> None:
        evidence.append(Evidence(id=f"e{len(evidence) + 1}", tool=tool, args=args, summary=summary[:evidence_chars]))

    # pods, and the drift scan (every baseline key against what the running container sees)
    snap.pods, snap.drift = await scan_drift(tools, snap.unavailable)
    note("get_pod_status", {}, "; ".join(f"{p.name} {p.ready} {p.phase}" for p in snap.pods) or "no pods listed")
    drifted = [d for d in snap.drift if d.state == "drift"]
    note("drift scan (list_baseline + get_live_config_value)", {},
         f"{len(drifted)} of {len(snap.drift)} keys drifted" + "".join(f"; {d.key}: golden={d.golden} live={d.live}" for d in drifted))

    # license and certificates
    lic = await _try(tools, "get_pf_license", {}, snap.unavailable)
    certs = await _try(tools, "get_pf_certificates", {}, snap.unavailable)
    if lic:
        snap.license_days = lic.get("days_until_expiry")
    if certs:
        days = [c["days_until_expiry"] for cat in certs.values() if isinstance(cat, list) for c in cat if c.get("days_until_expiry") is not None]
        snap.cert_days = min(days) if days else None
    note("get_pf_license / get_pf_certificates", {}, f"license {snap.license_days} days; soonest certificate {snap.cert_days} days")

    # PF's own view of the cluster
    cluster = await _try(tools, "get_pf_cluster_status", {}, snap.unavailable)
    if cluster:
        states = {n["replicationStatus"] for n in cluster["nodes"]}
        snap.replication = "SUCCEEDED" if states == {"SUCCEEDED"} and not cluster.get("replicationRequired") else ",".join(sorted(states))
    note("get_pf_cluster_status", {}, f"replication {snap.replication}")

    # how noisy are the logs right now
    logs = await _try(tools, "get_logs", {"level": "ERROR", "limit": 50}, snap.unavailable)
    snap.recent_errors = len(logs) if logs is not None else None
    note("get_logs", {"level": "ERROR", "limit": 50}, f"{snap.recent_errors} recent ERROR lines")

    return snap, evidence


def format_evidence(evidence: list[Evidence]) -> str:
    return "\n".join(f"{e.id}  {e.tool}: {e.summary}" for e in evidence) or "(none yet)"
