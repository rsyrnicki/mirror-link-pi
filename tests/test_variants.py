from __future__ import annotations

import json

import pytest

from mlpi import variants as vm
from mlpi.session import Session


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _mgr(tmp_path, clock, **kw) -> vm.VariantManager:
    vs = [vm.Variant(name="a"), vm.Variant(name="b"), vm.Variant(name="c")]
    kw.setdefault("cycles_per_variant", 1)
    return vm.VariantManager(vs, clock=clock, state_dir=tmp_path, **kw)


def _mib2_cycle(mgr, clock, gap=2.68):
    """One MIB2 handshake loop as recorded on 2026-09-28: no pause between cycles."""
    clock.t += gap
    mgr.on_request(is_root_descriptor=True, user_agent="QNX")      # GET /
    for _ in range(3):                                              # scpd, profile x2
        clock.t += 0.1
        mgr.on_request(is_root_descriptor=False)
    clock.t += 0.1
    mgr.on_request(is_root_descriptor=False)                        # GetApplicationList
    mgr.progress("applist")


def _attempt(mgr, clock, *, ua="QNX/6.5.0", launch=True):
    clock.t += 10
    mgr.on_request(is_root_descriptor=True, user_agent=ua)
    clock.t += 0.5
    mgr.on_request(is_root_descriptor=False)
    if launch:
        mgr.progress("launch")


def test_shipped_variants_file_loads():
    loaded = vm.load_variants()
    spec = next(v for v in loaded if v.name == "spec-1.0")
    assert spec == vm.Variant(name="spec-1.0", description=spec.description)
    # The bare spec-1.0 entry stalled the MIB2 after GetApplicationList: not first.
    assert loaded[0].name != "spec-1.0"
    assert len({v.name for v in loaded}) == len(loaded)


def test_rotation_advances_per_attempt(tmp_path):
    clock = FakeClock()
    mgr = _mgr(tmp_path, clock)
    seen = []
    for _ in range(4):
        _attempt(mgr, clock)
        seen.append(mgr.current.name)
    assert seen == ["a", "b", "c", "a"]


def test_requests_inside_an_attempt_do_not_rotate(tmp_path):
    clock = FakeClock()
    mgr = _mgr(tmp_path, clock)
    _attempt(mgr, clock)
    clock.t += 30  # long silence, but not a descriptor fetch
    mgr.on_request(is_root_descriptor=False)
    assert mgr.current.name == "a"
    clock.t += 1   # descriptor fetch right after other traffic: same attempt
    mgr.on_request(is_root_descriptor=True)
    assert mgr.current.name == "a" and mgr.attempt == 1


def test_vnc_after_launch_locks_and_persists_winner(tmp_path):
    clock = FakeClock()
    mgr = _mgr(tmp_path, clock)
    _attempt(mgr, clock)
    _attempt(mgr, clock)
    mgr.vnc_connected()
    assert mgr.locked and mgr.current.name == "b"
    _attempt(mgr, clock)
    assert mgr.current.name == "b"
    assert (tmp_path / "winner-variant").read_text().strip() == "b"
    # Next boot starts with the winner.
    assert _mgr(tmp_path, FakeClock()).current.name == "b"


def test_vnc_without_launch_or_from_simulator_does_not_lock(tmp_path):
    clock = FakeClock()
    mgr = _mgr(tmp_path, clock)
    _attempt(mgr, clock, launch=False)
    mgr.vnc_connected()
    assert not mgr.locked
    _attempt(mgr, clock, ua=f"x {vm.SIMULATOR_USER_AGENT}")
    mgr.vnc_connected()
    assert not mgr.locked
    assert not (tmp_path / "winner-variant").exists()


def test_fixed_mode(tmp_path):
    clock = FakeClock()
    mgr = _mgr(tmp_path, clock, mode="fixed", fixed_variant="c")
    _attempt(mgr, clock)
    _attempt(mgr, clock)
    assert mgr.current.name == "c"
    with pytest.raises(ValueError):
        _mgr(tmp_path, clock, mode="fixed", fixed_variant="nope")


def test_attempts_are_logged(tmp_path):
    session = Session(tmp_path / "s")
    clock = FakeClock()
    mgr = vm.VariantManager([vm.Variant(name="a"), vm.Variant(name="b")], clock=clock,
                            session=session)
    _attempt(mgr, clock)
    session.close()
    kinds = [json.loads(line)["kind"] for line in (tmp_path / "s/events.jsonl").open()]
    assert "attempt_start" in kinds and "attempt_progress" in kinds


def test_fast_looping_car_still_rotates(tmp_path):
    # The real car retried every ~2.7 s (< attempt_gap_seconds): the old silence-only
    # rule kept spec-1.0 for all 151 cycles. A finished app list must end the attempt.
    clock = FakeClock()
    mgr = _mgr(tmp_path, clock, cycles_per_variant=2)
    seen = []
    for _ in range(7):
        _mib2_cycle(mgr, clock)
        seen.append(mgr.current.name)
    assert seen == ["a", "a", "b", "b", "c", "c", "a"]
    assert mgr.attempt == 7


def test_vnc_after_next_cycle_started_credits_launching_variant(tmp_path):
    clock = FakeClock()
    mgr = _mgr(tmp_path, clock)
    _mib2_cycle(mgr, clock)
    mgr.progress("launch")          # variant "a" hands out the AppURI
    _mib2_cycle(mgr, clock)         # car starts over before connecting; now on "b"
    assert mgr.current.name == "b"
    clock.t += 1
    mgr.vnc_connected()
    assert mgr.locked and mgr.current.name == "a"
    assert (tmp_path / "winner-variant").read_text().strip() == "a"
