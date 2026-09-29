"""Device Attestation Protocol (DAP) endpoint — honest stub.

ETSI TS 103 544-4: a MirrorLink >= 1.1 client launches the DAP endpoint listed in the
app list (protocolID "DAP"), connects over TCP and sends an <attestationRequest>;
the server answers with an <attestationResponse> signed with a device key that chains
to the CCC root. We have no such key, so we cannot pass attestation.

What we can do is answer promptly and truthfully with result 1 ("Component not
existing or attestation not available", Table 5). Per Part 4 the client then treats
the device as uncertified: no content in *drive* mode, but a parked car may still
show it. Every request is recorded — it tells us the client's MirrorLink version,
its trust root hash and which components it wants attested.

Framing: Part 4 defines no length prefix; messages are bare XML documents on the TCP
stream. We read until the closing </attestationRequest> tag.
"""

from __future__ import annotations

import logging
import re
import socket
import threading

from .session import Session

log = logging.getLogger(__name__)

_END = re.compile(rb"</(?:\w+:)?attestationRequest\s*>")
RESULT_NOT_AVAILABLE = 1


def _field(xml: str, name: str) -> str:
    m = re.search(rf"<(?:\w+:)?{name}>\s*([^<]*?)\s*</(?:\w+:)?{name}>", xml)
    return m.group(1) if m else ""


def parse_request(xml: str) -> dict[str, str]:
    return {
        "major": _field(xml, "majorVersion"),
        "minor": _field(xml, "minorVersion"),
        "trust_root": _field(xml, "trustRoot"),
        "nonce": _field(xml, "nonce"),
        "component": _field(xml, "componentID"),
    }


def render_response(major: str = "1", minor: str = "0", result: int = RESULT_NOT_AVAILABLE
                    ) -> bytes:
    """attestationResponse per Part 4 Table 3 (attestation elements only on success)."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<attestationResponse>"
        f"<version><majorVersion>{major}</majorVersion>"
        f"<minorVersion>{minor}</minorVersion></version>"
        f"<result>{result}</result>"
        "</attestationResponse>"
    ).encode()


class DapServer:
    def __init__(self, *, bind_address: str, port: int, server_version: str = "1.1",
                 session: Session | None = None) -> None:
        self.bind_address = bind_address
        self.port = port
        self.server_version = server_version
        self.session = session
        self._stop = threading.Event()

    def serve_forever(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # Part 4 §4.1.3
        sock.bind((self.bind_address, self.port))
        sock.listen(2)
        sock.settimeout(1.0)
        log.info("DAP stub listening on %s:%d", self.bind_address, self.port)
        while not self._stop.is_set():
            try:
                conn, addr = sock.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                log.warning("DAP accept failed: %s", exc)
                self._stop.wait(1.0)
                continue
            threading.Thread(target=self._serve, args=(conn, addr), daemon=True).start()
        sock.close()

    def stop(self) -> None:
        self._stop.set()

    def _event(self, kind: str, **fields) -> None:
        if self.session:
            self.session.event(kind, **fields)

    def _serve(self, conn: socket.socket, addr) -> None:
        peer = f"{addr[0]}:{addr[1]}"
        log.info("DAP connection from %s", peer)
        self._event("dap_connect", peer=peer)
        buf = b""
        conn.settimeout(30.0)
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while (m := _END.search(buf)) is not None:
                    request, buf = buf[:m.end()], buf[m.end():]
                    text = request.decode("utf-8", "replace")
                    info = parse_request(text)
                    log.info("DAP request from %s: %s", peer, info)
                    self._event("dap_request", peer=peer, xml=text[:8192], **info)
                    # Answer with a version not higher than either side's (Part 4).
                    major, _, minor = self.server_version.partition(".")
                    if info["major"] and info["minor"]:
                        if (int(info["major"]), int(info["minor"])) < (int(major), int(minor)):
                            major, minor = info["major"], info["minor"]
                    conn.sendall(render_response(major, minor or "0"))
                    self._event("dap_response", peer=peer, result=RESULT_NOT_AVAILABLE)
        except (OSError, ValueError) as exc:
            log.info("DAP connection %s ended: %s", peer, exc)
        finally:
            conn.close()
            self._event("dap_disconnect", peer=peer, unparsed=buf[:512].decode("latin-1"))
