"""HTTP server that delivers the UPnP root device descriptor and SCPDs.

Every request the head unit makes that we do NOT serve a "real" answer for is logged
in full (method, path, headers, body). Per Robert's README the project explicitly aims
to "save all UPnP messages for further analysis" — this server is that surface.

Routes:
  GET  /                       → root device descriptor (rendered from template)
  GET  /scpd/<name>.xml        → SCPD file from config/service-descriptors/
  GET  /icon/<name>.png        → 128×128 PNG icon (used inside AppListing)
  GET  /presentation           → a tiny HTML stub
  POST /ctrl/*                 → SOAP dispatch (see soap.py)
  *    /evt/*                  → 501, body logged
  *    everything else         → 404, request logged
"""

from __future__ import annotations

import logging
import os
import struct
import zlib
from html import escape as _xml_escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from string import Template

from . import eventing, soap
from .config import Config

log = logging.getLogger(__name__)

# Repo layout: config/ sits next to src/mlpi/, so two parents up from this file.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_TEMPLATE = _REPO_ROOT / "config" / "device-descriptor.xml.tmpl"
DEFAULT_SCPD_DIR = _REPO_ROOT / "config" / "service-descriptors"


def render_descriptor(cfg: Config, address: str, *, template_path: Path = DEFAULT_TEMPLATE) -> str:
    template = Template(template_path.read_text(encoding="utf-8"))
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
    )


class DescriptorServer(ThreadingHTTPServer):
    """ThreadingHTTPServer that knows about its mlpi config.

    The ``cfg``, ``address`` and ``scpd_dir`` attributes are read by the handler — this
    is the standard stdlib pattern for parameterizing a BaseHTTPRequestHandler without
    subclassing per-instance.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        cfg: Config,
        address: str,
        *,
        scpd_dir: Path = DEFAULT_SCPD_DIR,
        template_path: Path = DEFAULT_TEMPLATE,
    ) -> None:
        super().__init__((address, cfg.network.http_port), DescriptorHandler)
        self.cfg = cfg
        self.address = address
        self.scpd_dir = scpd_dir
        self.template_path = template_path
        self.profile_store = soap.ProfileStore()
        self.app_status = soap.AppStatusStore()
        self.subscription_store = eventing.SubscriptionStore()
        self.soap_ctx = soap.ServerContext(
            profile_store=self.profile_store,
            app_status=self.app_status,
            subscription_store=self.subscription_store,
            address=address,
            http_port=cfg.network.http_port,
            vnc_port=cfg.network.vnc_port,
            app_name=cfg.device.friendly_name + " Display",
        )
        # Generate the AppListing icon once at startup. 128×128 24-bit PNG per
        # Part 9 §4.2.7 ("First icon shall be... width=128, height=128, depth=24").
        self.icon_png = _build_icon_png(128, 128)


class DescriptorHandler(BaseHTTPRequestHandler):
    server: DescriptorServer  # type: ignore[assignment]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib name
        log.info("%s - %s", self.address_string(), fmt % args)

    # ----- routes -----

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if self.path in ("/", "/description.xml", "/device.xml"):
            self._serve_root()
        elif self.path.startswith("/scpd/"):
            self._serve_scpd(self.path[len("/scpd/"):])
        elif self.path.startswith("/icon/"):
            self._serve_icon()
        elif self.path.startswith("/cert/"):
            self._serve_cert()
        elif self.path == "/presentation":
            self._serve_presentation()
        else:
            self._log_unknown("GET")
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        # SOAP control requests on /ctrl/* land here.
        if self.path.lstrip("/").startswith("ctrl/"):
            self._handle_soap_control()
            return
        self._log_unknown("POST")
        self.send_error(501, "Not Implemented")

    def _handle_soap_control(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        soap_action = self.headers.get("SOAPAction")
        try:
            req = soap.parse_soap(body, soap_action)
        except soap.SoapFault as fault:
            log.warning("SOAP parse fault on %s: %s", self.path, fault)
            self._send_soap_fault(fault, action=None)
            return
        except Exception as exc:
            log.exception("SOAP parse crashed on %s: body=%r", self.path, body[:300])
            self._send_soap_fault(soap.SoapFault(500, str(exc)), action=None)
            return

        log.info(
            "SOAP %s on %s args=%s body_size=%d",
            req.action, self.path, list(req.args.keys()), len(body)
        )
        # Dump arg values (truncated) so we can see exactly what the head unit
        # sends — useful when chasing down state-machine errors that don't show
        # up as protocol-level faults.
        for k, v in req.args.items():
            preview = v if len(v) <= 200 else v[:200] + f"... ({len(v)} bytes)"
            log.debug("  arg %s = %r", k, preview)
        try:
            resp = soap.dispatch(req, self.server.soap_ctx)
        except soap.SoapFault as fault:
            log.warning("SOAP fault on %s#%s: %s", req.service_urn, req.action, fault)
            self._send_soap_fault(fault, action=req.action)
            return
        except Exception as exc:
            log.exception("SOAP handler crashed on %s#%s", req.service_urn, req.action)
            self._send_soap_fault(soap.SoapFault(500, str(exc)), action=req.action)
            return

        body_out = soap.render_response(req, resp)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body_out)))
        self.send_header("Server", "Linux/UPnP/1.0 mlpi/0.1")
        self.send_header("EXT", "")
        self.end_headers()
        self.wfile.write(body_out)

    def _send_soap_fault(self, fault: soap.SoapFault, *, action: str | None) -> None:
        body = soap.render_fault(action, fault)
        self.send_response(500)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("EXT", "")
        self.end_headers()
        self.wfile.write(body)

    def do_SUBSCRIBE(self) -> None:  # noqa: N802 - stdlib name
        # GENA event subscription on /evt/<service>. We register the callback
        # URLs into the SubscriptionStore so SOAP handlers can fire NOTIFYs
        # when evented state variables change. The MIB II head unit will hang
        # at "Error: App" after LaunchApplication if it never sees the
        # AppStatusUpdate NOTIFY — polling GetApplicationStatus is not enough.
        if not self.path.lstrip("/").startswith("evt/"):
            self._log_unknown("SUBSCRIBE")
            self.send_error(404, "Not Found")
            return
        callbacks = eventing.parse_callback_header(self.headers.get("CALLBACK"))
        # Normalise path: drop trailing slashes, ensure leading slash. Used as
        # the key into the SubscriptionStore.
        service_path = "/" + self.path.lstrip("/").rstrip("/")
        if not callbacks:
            log.warning(
                "%s SUBSCRIBE %s — no parseable CALLBACK header, returning 412",
                self.address_string(), self.path,
            )
            self.send_error(412, "Precondition Failed")
            return
        sub = self.server.subscription_store.add(service_path, callbacks)
        timeout = self.headers.get("TIMEOUT", "Second-1800")
        log.info(
            "%s SUBSCRIBE %s sid=%s callbacks=%s",
            self.address_string(), self.path, sub.sid, callbacks,
        )
        self.send_response(200)
        self.send_header("SID", sub.sid)
        self.send_header("TIMEOUT", timeout)
        self.send_header("Server", "Linux/UPnP/1.0 mlpi/0.1")
        self.send_header("Content-Length", "0")
        self.end_headers()

        # UPnP §4.3.2 mandates an initial NOTIFY (SEQ=0) right after SUBSCRIBE
        # carrying the current value of every evented state variable.
        #
        # Per Part 9 §4.2.2 / §4.2.3: AppListUpdate and AppStatusUpdate values
        # are comma-separated lists of A_ARG_TYPE_AppID — NOT the AppList XML
        # body, NOT the AppStatus XML block. The first issuance of either
        # event after SUBSCRIBE shall list every appID in the current AppList
        # (so the client knows which apps to query).
        if service_path == "/evt/TmApplicationServer":
            ctx = self.server.soap_ctx
            initial_value = soap.VNC_APP_ID  # comma-separated; we have one app
            eventing.fire_event(
                ctx.subscription_store, service_path,
                [("AppListUpdate", initial_value),
                 ("AppStatusUpdate", initial_value)],
                delay=0.1,
            )

    def do_UNSUBSCRIBE(self) -> None:  # noqa: N802 - stdlib name
        if not self.path.lstrip("/").startswith("evt/"):
            self._log_unknown("UNSUBSCRIBE")
            self.send_error(404, "Not Found")
            return
        sid = self.headers.get("SID")
        if sid:
            self.server.subscription_store.remove(sid)
            log.info("%s UNSUBSCRIBE %s sid=%s", self.address_string(), self.path, sid)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ----- handlers -----

    def _serve_root(self) -> None:
        body = render_descriptor(
            self.server.cfg, self.server.address, template_path=self.server.template_path
        ).encode("utf-8")
        self._send_xml(body)

    def _serve_scpd(self, name: str) -> None:
        # Hard guard against path traversal — only allow plain filenames in the SCPD dir.
        target = (self.server.scpd_dir / name).resolve()
        if not str(target).startswith(str(self.server.scpd_dir.resolve())):
            self.send_error(403, "Forbidden")
            return
        if not target.is_file():
            self._log_unknown("GET", note=f"missing scpd: {name}")
            self.send_error(404, "Not Found")
            return
        self._send_xml(target.read_bytes())

    def _serve_icon(self) -> None:
        body = self.server.icon_png
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _serve_cert(self) -> None:
        # Per Part 9 §4.2.7 the head unit fetches this URL "for information
        # purpose" and "shall not validate the certificate's trust chain".
        # Session-3 final-shot experiment: instead of an XML stub, serve the
        # *real* CCC self-signed.ccc.crt (DER X.509, 3780 bytes) downloaded
        # from CarConnectivityConsortium/MirrorLink_Android_CommonAPI_TestApp.
        # If MIB II actually fetches this URL (it didn't in session 2 pre-
        # eventing) and parses the bytes, the CCC extension carrying the
        # app descriptor XML may unblock further launch progress.
        cert_path = Path("/etc/mlpi/self-signed.ccc.crt")
        try:
            body = cert_path.read_bytes()
        except FileNotFoundError:
            log.warning("cert file %s not found, returning 404", cert_path)
            self.send_error(404, "Cert file missing")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pkix-cert")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_presentation(self) -> None:
        body = (
            "<!doctype html><meta charset=utf-8>"
            f"<title>{self.server.cfg.device.friendly_name}</title>"
            f"<h1>{self.server.cfg.device.friendly_name}</h1>"
            f"<p>UDN: uuid:{self.server.cfg.ssdp.device_uuid}</p>"
            f"<p>Bound to {self.server.address}:{self.server.cfg.network.http_port}</p>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_xml(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # UPnP-required headers; head units check for these.
        self.send_header("Server", "Linux/UPnP/1.0 mlpi/0.1")
        self.end_headers()
        self.wfile.write(body)

    def _log_unknown(self, method: str, *, note: str = "") -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        header_dump = "\n".join(f"  {k}: {v}" for k, v in self.headers.items())
        log.warning(
            "Unhandled %s %s from %s%s\nHeaders:\n%s\nBody (%d bytes):\n%s",
            method,
            self.path,
            self.address_string(),
            f" ({note})" if note else "",
            header_dump,
            len(body),
            body.decode("utf-8", errors="replace"),
        )


# ---------- icon ----------

def _build_icon_png(width: int, height: int) -> bytes:
    """Build an in-memory 8-bit RGB PNG depicting a stylized monitor.

    Hand-rolled rather than pulling in Pillow because the icon is one fixed asset
    and we already need zlib for chunk compression. The image is a blue background
    with a centered white "screen" rectangle.
    """
    bg = (28, 90, 168)        # blue
    fg = (245, 245, 245)      # near-white "screen"
    margin = width // 8
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # PNG filter type 0 (None) for this scanline
        for x in range(width):
            inside = margin <= x < width - margin and margin <= y < height - margin
            r, g, b = fg if inside else bg
            rows.append(r)
            rows.append(g)
            rows.append(b)

    def chunk(typ: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(typ + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB, no interlace
    idat = zlib.compress(bytes(rows), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
