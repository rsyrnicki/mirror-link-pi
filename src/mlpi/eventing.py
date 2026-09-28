"""GENA event push for UPnP services.

Per UPnP Device Architecture §4.3 / ETSI TS 103 544-9: after a head unit
SUBSCRIBEs to /evt/<service>, the server pushes NOTIFY messages whenever an
evented state variable changes. The MIB II head unit waits for the
AppStatusUpdate NOTIFY after LaunchApplication and refuses to open the
advertised AppURI until it sees one. Without this module the head unit
hangs at "Error: App" because the polled GetApplicationStatus is not enough.
"""

from __future__ import annotations

import http.client
import logging
import os
import re
import time
from dataclasses import dataclass
from threading import Lock, Thread
from urllib.parse import urlparse

log = logging.getLogger(__name__)


@dataclass
class Subscription:
    sid: str
    callbacks: list[str]
    seq: int = 0


_CALLBACK_RE = re.compile(r"<([^>]+)>")


def parse_callback_header(value: str | None) -> list[str]:
    """Extract URLs from a UPnP CALLBACK header (`<url1><url2>...` form)."""
    if not value:
        return []
    return _CALLBACK_RE.findall(value)


class SubscriptionStore:
    """Thread-safe map of service path → list of active subscriptions."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._subs: dict[str, list[Subscription]] = {}

    def add(self, service_path: str, callbacks: list[str]) -> Subscription:
        sid = "uuid:" + os.urandom(16).hex()
        sub = Subscription(sid=sid, callbacks=list(callbacks))
        with self._lock:
            self._subs.setdefault(service_path, []).append(sub)
        log.info("subscription added: path=%s sid=%s callbacks=%s",
                 service_path, sid, callbacks)
        return sub

    def remove(self, sid: str) -> None:
        with self._lock:
            for path in list(self._subs.keys()):
                self._subs[path] = [s for s in self._subs[path] if s.sid != sid]

    def for_path(self, service_path: str) -> list[Subscription]:
        with self._lock:
            return list(self._subs.get(service_path, []))


def _xml_escape_text(value: str) -> str:
    return (value.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))


def render_propertyset(properties: list[tuple[str, str]]) -> bytes:
    """Build a UPnP propertyset NOTIFY body.

    Each (name, value) becomes ``<e:property><name>value</name></e:property>``.
    The value is treated as text content and XML-entity-escaped — UPnP
    evented state variables are typed (string in our case) and any nested XML
    inside the value gets character-data encoded.
    """
    parts = ['<?xml version="1.0"?>',
             '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">']
    for name, value in properties:
        parts.append(
            f'<e:property><{name}>{_xml_escape_text(value)}</{name}></e:property>'
        )
    parts.append('</e:propertyset>')
    return "".join(parts).encode("utf-8")


def _post_notify(callback: str, sid: str, seq: int, body: bytes,
                 delay: float = 0.0) -> None:
    if delay > 0:
        time.sleep(delay)
    try:
        u = urlparse(callback)
        if u.scheme != "http":
            log.warning("NOTIFY skipped — unsupported scheme: %s", callback)
            return
        host = u.hostname
        port = u.port or 80
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(
            "NOTIFY", path, body,
            {
                "HOST": f"{host}:{port}",
                "CONTENT-TYPE": 'text/xml; charset="utf-8"',
                "NT": "upnp:event",
                "NTS": "upnp:propchange",
                "SID": sid,
                "SEQ": str(seq),
                "Content-Length": str(len(body)),
            },
        )
        resp = conn.getresponse()
        log.info("NOTIFY → %s sid=%s seq=%d → HTTP %d",
                 callback, sid, seq, resp.status)
        resp.read()
        conn.close()
    except Exception as exc:
        log.warning("NOTIFY → %s failed: %s", callback, exc)


def fire_event(store: SubscriptionStore, service_path: str,
               properties: list[tuple[str, str]], delay: float = 0.05) -> None:
    """Push an event to all subscribers for the given service path.

    Spawns a daemon thread per (subscription × callback) so the caller is
    never blocked. The small ``delay`` before each POST gives a calling SOAP
    handler time to flush its response first — spec mandates AppStatusUpdate
    fire only after the LaunchApplication response has been sent.
    """
    body = render_propertyset(properties)
    subs = store.for_path(service_path)
    if not subs:
        log.info("fire_event: no subscribers for %s", service_path)
        return
    for sub in subs:
        seq = sub.seq
        sub.seq += 1
        for callback in sub.callbacks:
            Thread(target=_post_notify,
                   args=(callback, sub.sid, seq, body, delay),
                   daemon=True).start()
