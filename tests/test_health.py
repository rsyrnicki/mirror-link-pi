"""Pi health monitoring: supply alarms during start-up."""

from __future__ import annotations


def test_startup_power_alarm_is_written_at_once(monkeypatch):
    """Starts that ended ~22 s after power-on left no trace: a supply alarm in the
    first minute is recorded and synced to the card immediately."""
    import types

    from mlpi import health
    values = iter([0, 0, 0x50005, 0x50005, 0x50000])
    monkeypatch.setattr(health, "read_throttled", lambda: next(values, 0x50000))
    events, syncs = [], []
    session = types.SimpleNamespace(event=lambda kind, **f: events.append((kind, f)),
                                    sync=lambda: syncs.append(1))
    mon = health.HealthMonitor(session)
    monkeypatch.setattr(mon._stop, "wait", lambda t: False)
    clock = iter(range(0, 1000))
    monkeypatch.setattr(health.time, "monotonic", lambda: next(clock))
    mon._watch_startup_power(seconds=6)
    assert [f["throttled"] for k, f in events if k == "power"] == ["0x50005", "0x50000"]
    assert "undervoltage now" in events[0][1]["flags"] and len(syncs) == 2
