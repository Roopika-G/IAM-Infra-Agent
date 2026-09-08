import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.signature import compute_signature, normalize_message


def test_same_fault_same_signature_despite_different_ids():
    # Two occurrences of the "same" crash-loop error — different numbers/
    # addresses (as real log lines from repeated restarts would have),
    # same underlying fault. This is the entire point of normalization.
    msg1 = "Connection refused to jdbc:postgresql://10.244.0.43:5432/postgres after 30000ms"
    msg2 = "Connection refused to jdbc:postgresql://10.244.0.51:5432/postgres after 30012ms"
    sig1 = compute_signature("com.pingidentity.jdbc.Pool", "SQLException", msg1)
    sig2 = compute_signature("com.pingidentity.jdbc.Pool", "SQLException", msg2)
    assert sig1 == sig2


def test_different_fault_different_signature():
    sig1 = compute_signature("A", "SQLException", "Connection refused")
    sig2 = compute_signature("A", "FileNotFoundException", "Profile merge failed")
    assert sig1 != sig2


def test_logger_name_survives_normalization():
    # The bug we deliberately avoided: a naive path-stripping regex would
    # mangle dotted Java class/logger names. Confirm it doesn't.
    text = "Failed to load com.pingidentity.jgroups.ChannelFactory"
    assert "com.pingidentity.jgroups.ChannelFactory" in normalize_message(text)


def test_uuid_and_numbers_stripped():
    text = "Request 550e8400-e29b-41d4-a716-446655440000 failed after 3 retries"
    normalized = normalize_message(text)
    assert "550e8400" not in normalized
    assert "<uuid>" in normalized
    assert "<n>" in normalized
