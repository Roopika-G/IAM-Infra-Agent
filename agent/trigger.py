"""Which incidents start the agent.

A deliberately narrow allowlist: only Sim A's signature (PingFederate cannot
load its JDBC datastore). Only that fault has been validated end to end, so
only that fault starts the agent. Widening this one function is how later
issues are switched on; the graph does not change.
"""

from agent.schemas import Incident


def is_sim_a(incident: Incident) -> bool:
    return (
        (incident.logger or "").endswith("DataSourceManagerImpl")
        or "data source instance" in incident.sample_message.lower()
    )
