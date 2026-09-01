"""Desired-state conflict rules for manual VM lifecycle operations."""


def desired_conflict(desired, action, now, desired_on):
    """Reason a manual `action` ('start'|'stop'|'restart') will be reverted
    by the reconciler, or None.

    A CLI lifecycle op doesn't touch the desired record, so the reconciler
    keeps converging toward it: stopping a VM the schedule wants ON gets
    restarted next tick; starting one the schedule wants OFF gets stopped
    (after grace). 'restart' ends running, so it conflicts like 'start'.
    None for an unmanaged VM (no record, or an explicit 'unmanaged' record
    preserving a dormant schedule) or an action that agrees with the
    schedule. Pure — unit-testable; the I/O wrapper supplies the record."""
    if not desired or desired.get("mode") == "unmanaged":
        return None
    want_on = desired_on(desired, now)
    mode = desired.get("mode")
    if action == "stop" and want_on:
        return (f"scheduled ON (mode={mode}) — the reconciler will restart "
                f"it within ~2 min; set mode 'off' to keep it stopped")
    if action in ("start", "restart") and not want_on:
        return (f"scheduled OFF (mode={mode}) — the reconciler will stop it "
                f"again after grace; adjust the schedule to keep it running")
    return None
