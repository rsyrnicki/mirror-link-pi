from __future__ import annotations

import json

from mlpi import session as sm


def test_init_session_counts_boots_and_publishes_pointer(tmp_path):
    pointer = tmp_path / "run/session-dir"
    first = sm.init_session(tmp_path / "lib", pointer=pointer)
    second = sm.init_session(tmp_path / "lib", pointer=pointer)
    assert first.name == "0001" and second.name == "0002"
    assert sm.current_session_dir(pointer=pointer) == second
    assert (tmp_path / "lib/sessions/current").resolve() == second.resolve()


def test_stages_are_sticky_and_summarised(tmp_path):
    s = sm.Session(tmp_path / "0003")
    seen = []
    s.on_stage(seen.append)
    s.reach(sm.STAGE_UPNP)
    s.reach(sm.STAGE_USB_LINK)   # lower stage later: recorded, but stage stays
    s.reach(sm.STAGE_UPNP)       # repeat: ignored
    s.close()
    assert s.stage == sm.STAGE_UPNP
    assert seen == [sm.STAGE_BOOT, sm.STAGE_UPNP]
    events = [json.loads(line) for line in (tmp_path / "0003/events.jsonl").open()]
    assert [e["stage"] for e in events if e["kind"] == "stage"] == [3, 1]
    summary = (tmp_path / "0003/summary.txt").read_text()
    assert "boot #3" in summary and "FURTHEST STAGE: 3" in summary
