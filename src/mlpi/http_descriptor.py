"""HTTP server that delivers the UPnP root device descriptor, SCPDs and SOAP control.

Every request and every response is recorded in full (method, path, headers, body)
into the session event log — that is the data set we analyse after a car trip.

Routes:
  GET  /  (/description.xml)   → root device descriptor (rendered from template)
  GET  /scpd/<name>.xml        → SCPD file from config/service-descriptors/
  GET  /icon/<name>.png        → 128×128 PNG icon (used inside AppListing)
  GET  /cert/<name>            → CCC reference certificate, if installed
  GET  /presentation           → a tiny HTML stub
  POST /ctrl/*                 → SOAP dispatch (see soap.py)
  SUBSCRIBE/UNSUBSCRIBE /evt/* → GENA eventing (see eventing.py)
  *    everything else         → 404/501, request logged
"""

from __future__ import annotations

import logging
import struct
import zlib
from html import escape as _xml_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template

from . import eventing, soap
from .config import Config
from .session import STAGE_UPNP, Session
from .variants import Variant, VariantManager

log = logging.getLogger(__name__)

# Repo layout: config/ sits next to src/mlpi/, so two parents up from this file.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TEMPLATE = _REPO_ROOT / "config" / "device-descriptor.xml.tmpl"
DEFAULT_SCPD_DIR = _REPO_ROOT / "config" / "service-descriptors"
DEFAULT_CERT = Path("/etc/mlpi/self-signed.ccc.crt")

SERVER_HEADER = "Linux/UPnP/1.0 mlpi/0.2"
_ROOT_PATHS = ("/", "/description.xml", "/device.xml")
_BODY_LOG_LIMIT = 64 * 1024


def render_descriptor(cfg: Config, address: str, *, template_path: Path = DEFAULT_TEMPLATE,
                      variant: Variant | None = None) -> str:
    variant = variant or Variant()
    template = Template(template_path.read_text(encoding="utf-8"))
    extra = ""
    if variant.ml_version:
        major, _, minor = variant.ml_version.partition(".")
        extra = (
            "<X_mirrorLinkVersion>"
            f"<majorVersion>{_xml_escape(major)}</majorVersion>"
            f"<minorVersion>{_xml_escape(minor or '0')}</minorVersion>"
            "</X_mirrorLinkVersion>"
        )
    # All free-text values must be XML-escaped — manufacturer names and friendly names
    # routinely contain ``&`` which otherwise produces invalid XML.
    return template.substitute(
        address=_xml_escape(address, quote=True),
        http_port=str(cfg.network.http_port),
        device_type=_xml_escape(cfg.device.device_type),
        friendly_name=_xml_escape(cfg.device.friendly_name),
        manufacturer=_xml_escape(cfg.device.manufacturer),
        model_name=_xml_escape(cfg.device.model_name),
        model_number=_xml_escape(cfg.device.model_number),
        device_uuid=_xml_escape(cfg.ssdp.device_uuid),
        extra_device_xml=extra,
    )


class DescriptorServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that knows about its mlpi config, session and variants."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        cfg: Config,
        address: str,
        *,
        session: Session | None = None,
        variants: VariantManager | None = None,
        scpd_dir: Path = DEFAULT_SCPD_DIR,
        template_path: Path = DEFAULT_TEMPLATE,
        cert_path: Path = DEFAULT_CERT,
        bind_address: str | None = None,
    ) -> None:
        super().__init__((bind_address if bind_address is not None else address,
                          cfg.network.http_port), DescriptorHandler)
        self.cfg = cfg
        self.address = address
        self.session = session
        self.variants = variants
        self.scpd_dir = scpd_dir
        self.template_path = template_path
        self.cert_path = cert_path
        self.soap_ctx = soap.ServerContext(
            address=address,
            http_port=cfg.network.http_port,
            vnc_port=cfg.network.vnc_port,
            app_name=cfg.device.friendly_name + " Display",
            session=session,
            variant=(lambda: variants.current) if variants else Variant,
            progress=variants.progress if variants else (lambda step: None),
        )
        # 128×128 24-bit PNG per Part 9 §4.2.7 ("First icon shall be... width=128,
        # height=128, depth=24").
        self.icon_png = _build_icon_png(128, 128)

    def current_variant(self) -> Variant:
        return self.variants.current if self.variants else Variant()


class DescriptorHandler(BaseHTTPRequestHandler):
    server: DescriptorServer  # type: ignore[assignment]

    # One Server header, not BaseHTTP's plus ours.
    def version_string(self) -> str:
        return SERVER_HEADER

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib name
        log.info("%s - %s", self.address_string(), fmt % args)

    # ----- request plumbing -----

    def _read_body(self) -> bytes:
        if hasattr(self, "_body"):
            return self._body
        length = int(self.headers.get("Content-Length") or 0)
        self._body = self.rfile.read(length) if length > 0 else b""
        return self._body

    def _norm_path(self) -> str:
        # The car builds URLs as URLBase + "/ctrl/..." → "//ctrl/...". Normalise.
        return "/" + self.path.lstrip("/")

    def _record_request(self) -> None:
        session = self.server.session
        if session is None:
            return
        body = self._read_body()
        session.event(
            "http_request",
            client=self.client_address[0],
            method=self.command,
            path=self.path,
            headers=dict(self.headers.items()),
            body=body[:_BODY_LOG_LIMIT].decode("utf-8", "replace"),
            body_len=len(body),
            variant=self.server.current_variant().name,
        )

    def _reply(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)
        session = self.server.session
        if session is not None:
            is_text = not headers.get("Content-Type", "").startswith(("image/", "application/pkix"))
            session.event(
                "http_response",
                client=self.client_address[0],
                method=self.command,
                path=self.path,
                status=status,
                headers=headers,
                body=(body[:_BODY_LOG_LIMIT].decode("utf-8", "replace") if is_text
                      else f"<{len(body)} bytes binary>"),
                body_len=len(body),
            )

    def _error(self, status: int, message: str) -> None:
        self._reply(status, f"{status} {message}\n".encode(), {"Content-Type": "text/plain"})

    def _begin(self) -> str:
        """Common prologue for every method: attempt tracking + full request log."""
        self.__dict__.pop("_body", None)  # handler instances may serve >1 request
        path = self._norm_path()
        variants = self.server.variants
        if variants is not None:
            variants.on_request(
                is_root_descriptor=(self.command == "GET" and path in _ROOT_PATHS),
                user_agent=self.headers.get("User-Agent", ""),
            )
        self._record_request()
        return path

    # ----- routes -----

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        path = self._begin()
        if path in _ROOT_PATHS:
            self._serve_root()
        elif path.startswith("/scpd/"):
            self._serve_scpd(path[len("/scpd/"):])
        elif path.startswith("/icon/"):
            self._reply(200, self.server.icon_png,
                        {"Content-Type": "image/png", "Cache-Control": "public, max-age=86400"})
        elif path.startswith("/cert/"):
            self._serve_cert()
        elif path == "/presentation":
            self._serve_presentation()
        else:
            self._log_unknown("GET")
            self._error(404, "Not Found")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib name
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        path = self._begin()
        if path.startswith("/ctrl/"):
            self._handle_soap_control(path)
            return
        self._log_unknown("POST")
        self._error(501, "Not Implemented")

    def _handle_soap_control(self, path: str) -> None:
        body = self._read_body()
        soap_action = self.headers.get("SOAPAction")
        try:
            req = soap.parse_soap(body, soap_action)
        except soap.SoapFault as fault:
            log.warning("SOAP parse fault on %s: %s", path, fault)
            self._send_soap_fault(fault)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("SOAP parse crashed on %s: body=%r", path, body[:300])
            self._send_soap_fault(soap.SoapFault(500, str(exc)))
            return

        log.info("SOAP %s on %s args=%s variant=%s", req.action, path,
                 list(req.args.keys()), self.server.current_variant().name)
        try:
            resp = soap.dispatch(req, self.server.soap_ctx)
        except soap.SoapFault as fault:
            log.warning("SOAP fault on %s#%s: %s", req.service_urn, req.action, fault)
            self._send_soap_fault(fault)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("SOAP handler crashed on %s#%s", req.service_urn, req.action)
            self._send_soap_fault(soap.SoapFault(500, str(exc)))
            return

        self._reply(200, soap.render_response(req, resp),
                    {"Content-Type": 'text/xml; charset="utf-8"', "EXT": ""})

    def _send_soap_fault(self, fault: soap.SoapFault) -> None:
        self._reply(500, soap.render_fault(None, fault),
                    {"Content-Type": 'text/xml; charset="utf-8"', "EXT": ""})

    def do_SUBSCRIBE(self) -> None:  # noqa: N802 - stdlib name
        path = self._begin()
        if not path.startswith("/evt/"):
            self._log_unknown("SUBSCRIBE")
            self._error(404, "Not Found")
            return
        service_path = path.rstrip("/")
        timeout = self.headers.get("TIMEOUT", "Second-1800")
        store = self.server.soap_ctx.subscription_store

        sid = self.headers.get("SID")
        callbacks = eventing.parse_callback_header(self.headers.get("CALLBACK"))
        if sid and not callbacks:
            # Renewal (UDA §4.1.2): SID, no CALLBACK/NT.
            if store.renew(sid):
                self._reply(200, b"", {"SID": sid, "TIMEOUT": timeout})
            else:
                self._error(412, "Precondition Failed")
            return
        if not callbacks:
            log.warning("%s SUBSCRIBE %s — no parseable CALLBACK header, returning 412",
                        self.address_string(), path)
            self._error(412, "Precondition Failed")
            return
        sub = store.add(service_path, callbacks)
        self._reply(200, b"", {"SID": sub.sid, "TIMEOUT": timeout})

        # UPnP §4.3.2 mandates an initial NOTIFY (SEQ=0) right after SUBSCRIBE
        # carrying the current value of every evented state variable. Per Part 9
        # §4.2.2/§4.2.3 AppListUpdate/AppStatusUpdate are comma-separated AppID
        # lists; the first issuance lists every appID in the current AppList.
        if service_path == "/evt/TmApplicationServer":
            eventing.fire_event(
                store, service_path,
                [("AppListUpdate", soap.VNC_APP_ID), ("AppStatusUpdate", soap.VNC_APP_ID)],
                delay=0.1, session=self.server.session,
            )

    def do_UNSUBSCRIBE(self) -> None:  # noqa: N802 - stdlib name
        path = self._begin()
        if not path.startswith("/evt/"):
            self._log_unknown("UNSUBSCRIBE")
            self._error(404, "Not Found")
            return
        sid = self.headers.get("SID")
        if sid:
            self.server.soap_ctx.subscription_store.remove(sid)
        self._reply(200, b"", {})

    # ----- handlers -----

    def _serve_root(self) -> None:
        if self.server.session is not None:
            self.server.session.reach(STAGE_UPNP, client=self.client_address[0])
        body = render_descriptor(
            self.server.cfg, self.server.address,
            template_path=self.server.template_path,
            variant=self.server.current_variant(),
        ).encode("utf-8")
        self._reply(200, body, {"Content-Type": 'text/xml; charset="utf-8"'})

    def _serve_scpd(self, name: str) -> None:
        # Hard guard against path traversal — only allow plain filenames in the SCPD dir.
        target = (self.server.scpd_dir / name).resolve()
        if not str(target).startswith(str(self.server.scpd_dir.resolve())):
            self._error(403, "Forbidden")
            return
        if not target.is_file():
            self._log_unknown("GET", note=f"missing scpd: {name}")
            self._error(404, "Not Found")
            return
        self._reply(200, target.read_bytes(), {"Content-Type": 'text/xml; charset="utf-8"'})

    def _serve_cert(self) -> None:
        # Per Part 9 §4.2.7 the head unit fetches this URL "for information purpose"
        # and "shall not validate the certificate's trust chain". We serve the real
        # CCC self-signed.ccc.crt (DER X.509) from the CCC reference repo.
        try:
            body = self.server.cert_path.read_bytes()
        except OSError:
            log.warning("cert file %s not found, returning 404", self.server.cert_path)
            self._error(404, "Cert file missing")
            return
        self._reply(200, body, {"Content-Type": "application/pkix-cert"})

    def _serve_presentation(self) -> None:
        cfg = self.server.cfg
        body = (
            "<!doctype html><meta charset=utf-8>"
            f"<title>{_xml_escape(cfg.device.friendly_name)}</title>"
            f"<h1>{_xml_escape(cfg.device.friendly_name)}</h1>"
            f"<p>UDN: uuid:{_xml_escape(cfg.ssdp.device_uuid)}</p>"
            f"<p>Variant: {_xml_escape(self.server.current_variant().name)}</p>"
        ).encode()
        self._reply(200, body, {"Content-Type": "text/html; charset=utf-8"})

    def _log_unknown(self, method: str, *, note: str = "") -> None:
        body = self._read_body()
        header_dump = "\n".join(f"  {k}: {v}" for k, v in self.headers.items())
        log.warning(
            "Unhandled %s %s from %s%s\nHeaders:\n%s\nBody (%d bytes):\n%s",
            method, self.path, self.address_string(),
            f" ({note})" if note else "",
            header_dump, len(body), body.decode("utf-8", errors="replace"),
        )


# ---------- icon ----------

def _build_icon_png(width: int, height: int) -> bytes:
    """Build an in-memory 8-bit RGB PNG depicting a stylized monitor.

    Hand-rolled rather than pulling in Pillow because the icon is one fixed asset
    and we already need zlib for chunk compression.
    """
    bg = (28, 90, 168)        # blue
    fg = (245, 245, 245)      # near-white "screen"
    margin = width // 8
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG filter type 0 (None) for this scanline
        for x in range(width):
            inside = margin <= x < width - margin and margin <= y < height - margin
            rows.extend(fg if inside else bg)
    return encode_png(width, height, bytes(rows))


def encode_png(width: int, height: int, filtered_rows: bytes) -> bytes:
    """Wrap pre-filtered 8-bit RGB scanlines (filter byte + w*3 bytes each) in a PNG."""
    def chunk(typ: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(typ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB, no interlace
    idat = zlib.compress(filtered_rows, 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
