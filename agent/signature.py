"""Computes a stable fingerprint for a PF log message.

The point: the same underlying fault, logged 50 times during a crash loop,
must produce the exact same signature every time — only the parts of the
message that are always different anyway (timestamps, IPs, numeric IDs)
get stripped before hashing.

Deliberately simple for now. We don't have real Sim A/B error samples yet
(neither simulation has been triggered and observed), so this is tuned
against generic log noise, not against a known fault shape. Revisit once
Sim A/B produce real error text to normalize against — don't over-fit to
guesses now.
"""

import hashlib
import re

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_IP_PORT_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
# No \b around \d+ deliberately: "30000ms" has no word-boundary between the
# digits and "ms" (both are \w), so a \b-bounded pattern would silently
# fail to match numbers immediately followed by a unit suffix — caught by
# tests/test_signature.py actually failing on exactly this case.
_NUMBER_RE = re.compile(r"\d+")

# Deliberately no path/class-name stripping: a naive path regex would also
# match dotted Java logger/class names (e.g. com.pingidentity.jgroups.
# ChannelFactory) embedded in exception messages, destroying exactly the
# information that distinguishes one fault from another.


def normalize_message(message: str) -> str:
    """Strip volatile substrings so the same fault normalizes identically
    across repeated occurrences."""
    text = message or ""
    text = _UUID_RE.sub("<uuid>", text)
    text = _IP_PORT_RE.sub("<addr>", text)
    text = _NUMBER_RE.sub("<n>", text)
    return text.strip()


def compute_signature(logger: str | None, exception_type: str | None, message: str) -> str:
    """A short, stable hex fingerprint for (logger, exception_type,
    normalized message). Not cryptographic — collision resistance at this
    project's scale (a handful of distinct fault types) only needs to be
    good enough to not collide by accident, 16 hex chars is plenty."""
    normalized = normalize_message(message)
    basis = f"{logger or ''}|{exception_type or ''}|{normalized}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
