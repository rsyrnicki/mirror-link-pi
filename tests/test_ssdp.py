from __future__ import annotations

from mlpi import ssdp
from mlpi.config import Config


def _cfg() -> Config:
    return Config()


def _parse_headers(msg: bytes) -> dict[str, str]:
    text = msg.decode("utf-8")
    lines = text.split("\r\n")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().upper()] = value.strip()
    return headers


def test_render_alive_contains_required_fields():
    msg = ssdp.render_alive(_cfg(), "http://192.168.7.2:8080/")
    assert msg.startswith(b"NOTIFY * HTTP/1.1\r\n")
    assert msg.endswith(b"\r\n\r\n")
    headers = _parse_headers(msg)
    assert headers["HOST"] == "239.255.255.250:1900"
    assert headers["NT"] == "upnp:rootdevice"
    assert headers["NTS"] == "ssdp:alive"
    assert headers["LOCATION"] == "http://192.168.7.2:8080/"
    assert headers["CACHE-CONTROL"] == "max-age=1800"
    assert "c8cba096" in headers["USN"]


def test_render_byebye_marks_byebye():
    msg = ssdp.render_byebye(_cfg())
    headers = _parse_headers(msg)
    assert headers["NTS"] == "ssdp:byebye"


def test_render_msearch_response_has_st_and_location():
    msg = ssdp.render_msearch_response(_cfg(), "http://192.168.7.2:8080/")
    assert msg.startswith(b"HTTP/1.1 200 OK\r\n")
    headers = _parse_headers(msg)
    assert headers["ST"] == "upnp:rootdevice"
    assert headers["LOCATION"] == "http://192.168.7.2:8080/"
    assert "c8cba096" in headers["USN"]
    # EXT must be present per UPnP 1.0 §1.2.3 even though it has no value.
    assert "EXT" in headers


def test_render_msearch_for_discovery():
    msg = ssdp.render_msearch(_cfg(), search_target="upnp:rootdevice", mx=2)
    assert msg.startswith(b"M-SEARCH * HTTP/1.1\r\n")
    headers = _parse_headers(msg)
    assert headers["MAN"] == '"ssdp:discover"'
    assert headers["MX"] == "2"
    assert headers["ST"] == "upnp:rootdevice"


def test_responder_construction_does_not_open_socket():
    """Sanity: building one shouldn't touch the network until serve_forever runs."""
    r = ssdp.SsdpResponder(_cfg(), address="127.0.0.1", location="http://127.0.0.1:8080/")
    assert r._sock is None
    r.stop()
