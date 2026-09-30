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


def test_throttled_flags_decode():
    from mlpi.health import decode_throttled
    assert decode_throttled(0x50005) == ["undervoltage now", "throttled now",
                                         "undervoltage has occurred", "throttling has occurred"]
    assert decode_throttled(0) == []


def test_prune_sessions_keeps_newest_and_their_pcaps(tmp_path):
    from mlpi.session import prune_sessions
    for n in range(1, 8):
        d = tmp_path / f"{n:04d}"
        d.mkdir()
        (d / "events.jsonl").write_text("{}")
        (d / "usb0.pcap").write_bytes(b"x")
    (tmp_path / "current").symlink_to("0007")
    prune_sessions(tmp_path, keep=5, keep_pcaps=2, min_free=0)
    left = sorted(p.name for p in tmp_path.iterdir() if not p.is_symlink())
    assert left == ["0003", "0004", "0005", "0006", "0007"]
    assert [p.parent.name for p in sorted(tmp_path.glob("0*/usb0.pcap"))] == ["0006", "0007"]
    assert (tmp_path / "0003" / "events.jsonl").exists()


def test_station_dump_parsing():
    from mlpi.health import parse_station_dump
    dump = """Station aa:8b:f6:42:18:cb (on wlan0)
\tinactive time:\t120 ms
\trx bytes:\t123456
\ttx retries:\t42
\ttx failed:\t3
\tsignal:  \t-48 [-48] dBm
\ttx bitrate:\t65.0 MBit/s MCS 7
\trx bitrate:\t72.2 MBit/s MCS 7 short GI
"""
    [st] = parse_station_dump(dump)
    assert st["mac"] == "aa:8b:f6:42:18:cb"
    assert st["signal"] == "-48 [-48] dBm" and st["tx_failed"] == "3"
    assert st["tx_bitrate"] == "65.0 MBit/s MCS" and st["inactive"] == "120 ms"
