"""Clock-skew classification thresholds, by node type.

Storage (s*) nodes run etcd and PostgreSQL, where tight time sync matters, so
they keep a strict band. Other node types only need the clock roughly right
(TLS validity, log ordering); macOS nodes in particular drift under their
native `timed` between syncs, so non-storage nodes get a 10x looser band to
avoid alert noise on offsets that don't actually break anything.

Thresholds are (warning_ms, critical_ms) and preserve the same 10x
warning:critical ratio across both bands.
"""

_STORAGE_THRESHOLDS = (100, 1000)
_NON_STORAGE_THRESHOLDS = (1000, 10000)


def clock_skew_thresholds(node_type):
    """Return (warning_ms, critical_ms) for the given node type."""
    if node_type == 'storage':
        return _STORAGE_THRESHOLDS
    return _NON_STORAGE_THRESHOLDS


def classify_clock_offset(offset_ms, node_type):
    """Classify a clock offset (in ms, signed) as healthy/warning/critical."""
    warning_ms, critical_ms = clock_skew_thresholds(node_type)
    magnitude = abs(offset_ms)
    if magnitude > critical_ms:
        return 'critical'
    if magnitude > warning_ms:
        return 'warning'
    return 'healthy'
