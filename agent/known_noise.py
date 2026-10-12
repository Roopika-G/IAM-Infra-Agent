"""PingFederate's known, harmless startup messages.

Every time a PF pod starts it logs these as WARN or ERROR even though nothing
is wrong with the deployment. Turning each into an incident after every
restart would bury the real faults and use up incident numbers, so the detector
skips them. This list is deliberately short and specific: a message is only
here if it was seen at every startup of a healthy cluster. A real fault
(for example "Unable to load custom data source instance") must never be added.
"""

import re

KNOWN_STARTUP_NOISE = [
    r"^pf\.runtime\.threads\.max is not set",
    r"^pf\.console\.authentication: .* is not a valid authentication type",
    r"^Channel already has an up handler installed",
    r"^Not using invalid host \(\$\{DISCOVERY_INITIAL_HOST\}\)",
    r"^Cluster encryption disabled and no value specified for the pf\.cluster\.auth\.pwd",
    r"^Unable to load OAUTH_ACCESS_TOKEN plugin instance: default",
]
_COMPILED = [re.compile(p) for p in KNOWN_STARTUP_NOISE]


def is_known_startup_noise(message: str) -> bool:
    return any(p.search(message or "") for p in _COMPILED)
