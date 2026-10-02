from mlpi.launcher import Launcher
from mlpi.phonestatus import PhoneState, network_name, parse_poll
from mlpi.video import VideoFrame

POLL = """Current Battery Service state:
  AC powered: false
  USB powered: true
  Wireless powered: false
  status: 2
  level: 82
===
NR_SA,Unknown
===
1
===
1790000000,+0200
===
  mSignalStrength=SignalStrength:{ mCdma=CellSignalStrengthCdma: level=0 mLte=CellSignalStrengthLte: rssi=-61 level=3 mNr=CellSignalStrengthNr:{ level=4 } primary=CellSignalStrengthNr}
  mSignalStrength=SignalStrength:{ mLte=CellSignalStrengthLte: level=0 }
"""  # noqa: E501 - verbatim dumpsys line


def test_parse_poll():
    st = parse_poll(POLL, now=100.0)
    assert st["battery"] == 82 and st["charging"] is True
    assert st["network"] == "5G" and st["signal"] == 4 and st["dnd"] is True
    # 1790000000 = 14:13:20 UTC → 16:13 at +0200, and it keeps ticking on the Pi
    assert PhoneState(clock_base=st["clock_base"]).clock(now=100.0) == "16:13"
    assert PhoneState(clock_base=st["clock_base"]).clock(now=100.0 + 7 * 3600) == "23:13"
    assert PhoneState(clock_base=st["clock_base"]).clock(now=100.0 + 8 * 3600) == "00:13"


def test_parse_poll_tolerates_missing_parts():
    st = parse_poll("level: 15\\n===\\n\\n===\\nnull\\n===\\n\\n===\\n")
    assert "dnd" not in st and "clock_base" not in st and st["signal"] is None
    assert network_name("LTE,Unknown") == "4G" and network_name("Unknown") == ""


def test_status_bar_redraws_on_change_only():
    launcher = Launcher(VideoFrame(800, 480), [])
    v = launcher.frame.version
    launcher.set_state(battery=50)
    assert launcher.frame.version == v + 1
    launcher.set_state(battery=50)                 # unchanged: no redraw
    assert launcher.frame.version == v + 1


def test_signal_level_on_5g():
    nr = ("mSignalStrength=SignalStrength:{mLte=CellSignalStrengthLte: rssi=2147483647 "
          "level=0 parametersUseForLevel=0,mNr=CellSignalStrengthNr:{ csiRsrp = -44 "
          "ssRsrp = -98 ssRsrq = -11 ssSinr = 12 level = 3 parametersUseForLevel = 0 }}")
    from mlpi.phonestatus import parse_signal
    assert parse_signal(nr) == 3
