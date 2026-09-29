from __future__ import annotations

import threading

from mlpi import gadget, runner
from mlpi.config import Config
from mlpi.session import Session


class _Ssdp:
    def announce(self) -> None:
        pass


def test_link_monitor_restarts_service_when_usb0_is_recreated(tmp_path, monkeypatch):
    indexes = iter([5, 5, 5, 9, 9, 9])
    monkeypatch.setattr(gadget, "_ifindex", lambda name: next(indexes, 9))
    monkeypatch.setattr(gadget, "carrier", lambda name: True)
    monkeypatch.setattr(gadget, "udc_state", lambda udc=None: "configured")
    session = Session(tmp_path / "0001")
    fired = threading.Event()
    monitor = runner.LinkMonitor(Config(), session, _Ssdp(), None,
                                 on_interface_recreated=fired.set)
    t = threading.Thread(target=monitor.run, daemon=True)
    t.start()
    assert fired.wait(5)
    t.join(2)
    session.close()
    assert not t.is_alive()
    assert '"usb_interface_recreated"' in (tmp_path / "0001/events.jsonl").read_text()


def test_context_info_follows_variant():
    from mlpi.variants import Variant
    assert runner.context_info(Variant()) == (1, 0x80, 0x00010001, 0)
    assert runner.context_info(Variant(home_app=True))[0] == 2
