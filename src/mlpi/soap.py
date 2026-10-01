"""SOAP-1.1 envelope handling for the MirrorLink UPnP control endpoints.

Implements the actions the head unit actually invokes (and a few it might) per
ETSI TS 103 544-9/-10. ProfileID is always "0" per spec.

  TmClientProfile:1#GetMaxNumProfiles      -> NumProfilesAllowed=1
  TmClientProfile:1#SetClientProfile       -> store + echo back as ResultProfile
  TmClientProfile:1#GetClientProfile       -> echo last stored ClientProfile
  TmApplicationServer:1#GetApplicationList -> AppListing with one VNC server entry
  TmApplicationServer:1#LaunchApplication  -> AppURI = vnc://<address>:<vnc_port>
  TmApplicationServer:1#TerminateApplication -> TerminationResult=true (idempotent)
  TmApplicationServer:1#GetApplicationStatus -> tracked Foreground/Notrunning
  TmApplicationServer:1#GetApplicationCertificateInfo -> informational cert block

String arguments carry XML documents (ClientProfile, AppListing, AppStatus). On the
wire they are entity-escaped exactly once. We unescape them when parsing and escape
them exactly once when rendering. Before 2026-09 the parser kept the escaped form and
the renderer escaped it again, so the head unit got ``&amp;lt;clientProfile…`` back
from SetClientProfile — i.e. text, not a profile.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape as _xml_escape

from . import eventing
from .session import STAGE_LAUNCH, Session
from .variants import Variant

log = logging.getLogger(__name__)


# Applications we can advertise. IDs must be non-zero per Part 9 §4.2.5.
VNC_APP_ID = "0x00000001"      # stand-alone VNC server (always listed)
HOME_APP_ID = "0x00000002"     # VNC "home screen" UI application (variant.home_app)
DAP_APP_ID = "0x00000003"      # Device Attestation Protocol endpoint (variant.dap)
VNC_APP_ID_INT = int(VNC_APP_ID, 16)
HOME_APP_ID_INT = int(HOME_APP_ID, 16)
DAP_APP_ID_INT = int(DAP_APP_ID, 16)

# RTP audio endpoints (variant.rtp_apps), shaped like a Galaxy S6's app list: an RTP
# server ("out", category 0xF0000001) and client ("in", 0xF0000002) per payload type
# the car announced (98, 99). (app ID, payload, direction, port)
RTP_APPS = [("0x00000005", 99, "out", 10500), ("0x00000006", 98, "out", 10500),
            ("0x00000007", 99, "in", 10600), ("0x00000008", 98, "in", 10600)]
RTP_APP_IDS_INT = {int(a, 16) for a, *_ in RTP_APPS}

# Bluetooth audio links (variant.bt_apps, Part 9 §5.2.3 Table 5-1): the phone's A2DP
# (music, out) and HFP (calls, both ways). (app ID, protocolID, direction, audioType)
BT_APPS = [("0x00000009", "BTA2DP", "out", "application"),
           ("0x0000000A", "BTHFP", "bi", "phone")]
BT_APP_IDS_INT = {int(a, 16): proto for a, proto, *_ in BT_APPS}

APPLIST_NS = "urn:schemas-upnp-org:tmapplicationserver:applist-1-0"


def advertised_app_ids(variant: Variant, bt_address: str = "") -> list[str]:
    ids = [VNC_APP_ID]
    if variant.home_app:
        ids.append(HOME_APP_ID)
    if variant.dap:
        ids.append(DAP_APP_ID)
    if variant.rtp_apps:
        ids += [a for a, *_ in RTP_APPS]
    if variant.bt_apps and bt_address:
        ids += [a for a, *_ in BT_APPS]
    return ids


def _parse_app_id(raw: str) -> int | None:
    """Parse an A_ARG_TYPE_AppID. Per §4.2.5 case + leading zeros are insignificant.

    Returns None if the value can't be parsed; callers should treat that as a
    malformed AppID (UPnP error 810).
    """
    s = raw.strip().lower()
    if not s.startswith("0x"):
        return None
    try:
        return int(s, 16)
    except ValueError:
        return None

# Default empty ClientProfile XML returned when nothing has been set yet (per Part 10
# §4.5.4: "if a profile has never been updated by any MirrorLink UPnP Control Point
# using the SetClientProfile action, then invocation of the GetClientProfile action
# using its profileID shall return the profile populated with default parameter
# values"). We use the absolute minimum.
DEFAULT_CLIENT_PROFILE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<clientProfile xmlns="urn:schemas-upnp-org:tmclientprofile:clientprofile-1-0">'
    "<clientID>default</clientID>"
    "<manufacturer>default</manufacturer>"
    "<modelNumber>0</modelNumber>"
    "</clientProfile>"
)


def render_app_listing(ctx: ServerContext, variant: Variant) -> str:
    """Build the A_ARG_TYPE_AppList for the active experiment ``variant``.

    Always: the stand-alone VNC server, identified per Part 9 §5.2.1 by protocolID
    "VNC" and appCategory "0xF0000001" (Server functionality). Optionally a VNC
    home-screen UI application and a DAP endpoint (§5.2.5: protocolID "DAP",
    appCategory "0xF0000001", format = MirrorLink version). Required per app
    (Table 4-3): appID, name, remotingInfo/protocolID; icons for VNC apps.
    1.0/1.1 servers may omit the Signature (§4.2.7 implementation note).
    """
    icon_url = f"http://{ctx.address}:{ctx.http_port}/icon/mlpi.png"
    cert_url = f"http://{ctx.address}:{ctx.http_port}/cert/mlpi.cert"
    icon = ('<iconList><icon><mimetype>image/png</mimetype>'
            '<width>128</width><height>128</height><depth>24</depth>'
            f'<url>{_xml_escape(icon_url)}</url></icon></iconList>')
    ns = f' xmlns="{APPLIST_NS}"' if variant.applist_namespace else ""
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', f'<appList{ns}>']

    # 1) stand-alone VNC server
    allowed = '<allowedProfileIDs>0</allowedProfileIDs>' if variant.allowed_profile_ids else ''
    parts += ['<app>', f'<appID>{VNC_APP_ID}</appID>',
              f'<name>{_xml_escape(ctx.app_name)}</name>', icon, allowed,
              '<remotingInfo><protocolID>VNC</protocolID></remotingInfo>']
    if variant.cert_url:
        parts.append(f'<appCertificateURL>{_xml_escape(cert_url)}</appCertificateURL>')
    parts.append('<appInfo>')
    parts.append(f'<appCategory>{_xml_escape(variant.app_category)}</appCategory>')
    if variant.app_trust_level:
        parts.append(f'<trustLevel>{_xml_escape(variant.app_trust_level)}</trustLevel>')
    parts.append('</appInfo>')
    if variant.display_content_category:
        parts.append(
            '<displayInfo>'
            f'<contentCategory>{_xml_escape(variant.display_content_category)}</contentCategory>'
            '</displayInfo>'
        )
    if variant.audio_info:
        parts.append('<audioInfo><audioType>all</audioType>'
                     '<contentCategory>0x80000000</contentCategory>')
        if variant.audio_trust_level:
            parts.append(f'<trustLevel>{_xml_escape(variant.audio_trust_level)}</trustLevel>')
        parts.append('</audioInfo>')
    # "free" = available, "busy" = in use (Part 9 Table 4-3). Reflect runtime status
    # so the list never contradicts GetApplicationStatus.
    busy = ctx.app_status.get(VNC_APP_ID_INT) in ("Foreground", "Background")
    parts.append(f'<resourceStatus>{"busy" if busy else "free"}</resourceStatus>')
    parts.append('</app>')

    # 2) home screen UI application, remoted over the same VNC server
    if variant.home_app:
        parts += ['<app>', f'<appID>{HOME_APP_ID}</appID>',
                  f'<name>{_xml_escape(ctx.home_app_name)}</name>',
                  '<description>MirrorLink-Pi status screen</description>', icon, allowed,
                  '<remotingInfo><protocolID>VNC</protocolID></remotingInfo>',
                  '<appInfo><appCategory>'
                  f'{_xml_escape(variant.context_app_category)}</appCategory>'
                  f'<trustLevel>{_xml_escape(variant.context_trust_level)}</trustLevel>'
                  '</appInfo>',
                  '<displayInfo><contentCategory>0x00000000</contentCategory>'
                  f'<trustLevel>{_xml_escape(variant.context_trust_level)}</trustLevel>'
                  '</displayInfo>',
                  '</app>']

    # 3) RTP audio endpoints (no audio is streamed yet: listing them is the experiment)
    if variant.rtp_apps:
        for app_id, payload, direction, _port in RTP_APPS:
            server = direction == "out"
            parts += ['<app>', f'<appID>{app_id}</appID>',
                      f'<name>RTP {"Server" if server else "Client"} {payload}</name>',
                      f'<description>RTP Audio {"Server" if server else "Client"}</description>',
                      allowed,
                      '<remotingInfo><protocolID>RTP</protocolID>'
                      f'<format>{payload}</format><direction>{direction}</direction>'
                      '<audioIPL>4800</audioIPL><audioMPL>9600</audioMPL></remotingInfo>',
                      '<appInfo><appCategory>'
                      f'{"0xF0000001" if server else "0xF0000002"}</appCategory>'
                      '<trustLevel>0x80</trustLevel></appInfo>',
                      '<audioInfo>'
                      f'<audioType>{"application" if server else "phone"}</audioType>'
                      f'<contentCategory>{"0x2" if server else "0x10"}</contentCategory>'
                      '<contentRules>0x0</contentRules><trustLevel>0x80</trustLevel>'
                      '</audioInfo>',
                      '<resourceStatus>free</resourceStatus>', '</app>']

    # 4) Bluetooth audio links: the phone's own A2DP/HFP, so the car can use the phone's
    #    Bluetooth for audio. No appCategory (Table 5-1: "-"); 1.0 clients may infer
    #    the audio type from the protocolID, but we state it anyway.
    bt = ctx.bt_address()
    if variant.bt_apps and bt:
        for app_id, proto, direction, audio_type in BT_APPS:
            parts += ['<app>', f'<appID>{app_id}</appID>',
                      f'<name>Bluetooth {"Audio" if proto == "BTA2DP" else "Phone"}</name>',
                      allowed,
                      f'<remotingInfo><protocolID>{proto}</protocolID>'
                      f'<direction>{direction}</direction></remotingInfo>',
                      f'<audioInfo><audioType>{audio_type}</audioType></audioInfo>',
                      '<resourceStatus>free</resourceStatus>', '</app>']

    # 5) Device Attestation Protocol endpoint
    if variant.dap:
        version = variant.ml_version or "1.0"
        parts += ['<app>', f'<appID>{DAP_APP_ID}</appID>', '<name>Device Attestation</name>',
                  '<remotingInfo><protocolID>DAP</protocolID>'
                  f'<format>{_xml_escape(version)}</format></remotingInfo>',
                  '<appInfo><appCategory>0xF0000001</appCategory></appInfo>',
                  '</app>']

    parts.append('</appList>')
    return "".join(parts)


def render_app_certificate(ctx: ServerContext) -> str:
    """Return an AppCertificateInfo block per Part 9 §5.5.5.

    The MirrorLink Client uses this for *information* — per the spec it shall not
    validate the trust chain. We claim broad certification (base + drive for all
    listed regions) to maximize the chance the head unit considers our app
    "non-restricted" in its current driving mode.
    """
    regions = "EU,EPE,RUS,CAN,USA,BRA,AMERICA,AUS,KOR,JPN,CHN,HKG,TPE,IND,APAC,AFRICA,WORLD"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<certification xml:id="mlAppCertificate">'
        f'<appID>{VNC_APP_ID}</appID>'
        '<nonce></nonce>'
        '<appUUID>uuid:c8cba096-5abe-47ac-9c14-3267d7c94ce6</appUUID>'
        '<entity>'
        '<name>CCC</name>'
        '<targetList><target></target></targetList>'
        f'<restricted>{regions}</restricted>'
        f'<nonRestricted>{regions}</nonRestricted>'
        '<serviceList><service></service></serviceList>'
        '</entity>'
        '<properties></properties>'
        '</certification>'
    )


@dataclass
class SoapRequest:
    """Parsed SOAP request: service URN + action name + extracted (unescaped) args."""
    service_urn: str
    action: str
    args: dict[str, str]


@dataclass
class SoapResponse:
    """SOAP response payload: ordered (name, value) pairs to render into the body."""
    args: list[tuple[str, str]]


class SoapFault(Exception):
    """Raise to make the handler return a SOAP fault with the given UPnP errorCode."""

    def __init__(self, code: int, description: str = "") -> None:
        super().__init__(f"{code} {description}")
        self.code = code
        self.description = description


class ProfileStore:
    """In-memory store for client profiles (unescaped XML). Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._profiles: dict[str, str] = {}  # profileID → ClientProfile XML

    def set(self, profile_id: str, profile_xml: str) -> None:
        with self._lock:
            self._profiles[profile_id] = profile_xml
            log.info(
                "ProfileStore: stored profile %r (%d bytes)", profile_id, len(profile_xml)
            )

    def get(self, profile_id: str) -> str:
        with self._lock:
            return self._profiles.get(profile_id, DEFAULT_CLIENT_PROFILE_XML)


class AppStatusStore:
    """Tracks per-app status (Foreground/Background/Notrunning).

    The VW MIB II observed pattern is: it calls LaunchApplication, then immediately
    calls GetApplicationStatus, and only proceeds if it sees Foreground. So we flip
    the status to Foreground synchronously when LaunchApplication returns; the VNC
    server itself runs continuously.
    """

    _DEFAULT = "Notrunning"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status: dict[int, str] = {}  # AppID-as-int → statusType

    def set(self, app_id: int, status: str) -> None:
        with self._lock:
            self._status[app_id] = status
            log.info("AppStatusStore: app 0x%x is now %s", app_id, status)

    def get(self, app_id: int) -> str:
        with self._lock:
            return self._status.get(app_id, self._DEFAULT)

    def reset(self) -> None:
        with self._lock:
            self._status.clear()


@dataclass
class ServerContext:
    """Runtime context shared with SOAP handlers.

    The address + ports are needed to build URLs that the head unit will hand out
    to itself (the icon URL inside AppListing, the AppURI returned by
    LaunchApplication). They must match the actual bind addresses, so we plumb them
    through from the HTTP server rather than hard-coding.
    """
    address: str
    http_port: int
    vnc_port: int
    app_name: str
    home_app_name: str = "MirrorLink Pi"
    dap_port: int = 5510
    profile_store: ProfileStore = field(default_factory=ProfileStore)
    app_status: AppStatusStore = field(default_factory=AppStatusStore)
    subscription_store: eventing.SubscriptionStore = field(
        default_factory=eventing.SubscriptionStore)
    session: Session | None = None
    # Returns the variant for the current attempt; default = spec defaults.
    variant: Callable[[], Variant] = Variant
    # Called with a step name when the attempt progresses (e.g. "launch").
    progress: Callable[[str], None] = lambda step: None
    # The phone's Bluetooth address (12 hex digits) for bt_apps variants; "" = unknown.
    bt_address: Callable[[], str] = lambda: ""


_TM_APP_EVT_PATH = "/evt/TmApplicationServer"


def render_app_status_value(app_ids: list[str]) -> str:
    """Build the AppStatusUpdate / AppListUpdate event value.

    Per Part 9 §4.2.2: a UTF-8 string holding a *comma-separated list* of
    A_ARG_TYPE_AppID values whose status has changed — NOT the full AppStatus XML
    block. The head unit, on receiving the event, calls GetApplicationStatus on the
    listed appIDs to fetch detail.
    """
    return ",".join(app_ids)


# ---------- parsing ----------

# Per SOAP spec the SOAPAction header is mandatory. Format:
#   "urn:schemas-upnp-org:service:Foo:1#ActionName"
_SOAP_ACTION_RE = re.compile(r'^"?(?P<urn>[^"#]+)#(?P<action>[^"]+)"?$')


def parse_soap(body: bytes, soap_action_header: str | None) -> SoapRequest:
    """Parse a SOAP envelope into action + args.

    Uses regex on the raw text rather than ``xml.etree`` so that arguments which
    carry XML documents are captured whether the client escaped them (spec) or sent
    them as nested raw XML. Values are entity-unescaped once.

    The action name and service URN are taken from the ``SOAPAction`` header (mandatory
    per UPnP/SOAP). The argument names + values are extracted from the action block in
    the body.
    """
    if not soap_action_header:
        raise SoapFault(401, "Missing SOAPAction header")
    match = _SOAP_ACTION_RE.match(soap_action_header.strip())
    if not match:
        raise SoapFault(401, f"Malformed SOAPAction header: {soap_action_header!r}")
    urn = match.group("urn")
    action = match.group("action")

    text = body.decode("utf-8", errors="replace")
    # Pull child elements of the action — anything between <NS:Action> and </NS:Action>.
    # We don't care about the NS prefix; match any prefix followed by `:Action`.
    action_block_re = re.compile(
        rf"<\w+:{re.escape(action)}\b[^>]*>(.*?)</\w+:{re.escape(action)}>",
        re.DOTALL,
    )
    block_match = action_block_re.search(text)
    args: dict[str, str] = {}
    if block_match:
        inner = block_match.group(1)
        for arg_match in re.finditer(r"<(\w+)(?:\s[^>]*)?>(.*?)</\1>", inner, re.DOTALL):
            args[arg_match.group(1)] = html.unescape(arg_match.group(2))

    return SoapRequest(service_urn=urn, action=action, args=args)


# ---------- rendering ----------

def render_response(req: SoapRequest, resp: SoapResponse) -> bytes:
    """Render a SOAP/UPnP success response for the given request."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"',
        '  s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">',
        " <s:Body>",
        f'  <u:{req.action}Response xmlns:u="{req.service_urn}">',
    ]
    for name, value in resp.args:
        # Values are plain strings; escape them exactly once (no quote escaping needed
        # inside element content).
        escaped = _xml_escape(value, quote=False)
        lines.append(f"   <{name}>{escaped}</{name}>")
    lines.extend([f"  </u:{req.action}Response>", " </s:Body>", "</s:Envelope>", ""])
    return "\n".join(lines).encode("utf-8")


def render_fault(req_action: str | None, fault: SoapFault) -> bytes:
    """Render a UPnP-style SOAP fault."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"\n'
        '  s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">\n'
        " <s:Body>\n"
        "  <s:Fault>\n"
        "   <faultcode>s:Client</faultcode>\n"
        "   <faultstring>UPnPError</faultstring>\n"
        "   <detail>\n"
        '    <UPnPError xmlns="urn:schemas-upnp-org:control-1-0">\n'
        f"     <errorCode>{fault.code}</errorCode>\n"
        f"     <errorDescription>{_xml_escape(fault.description)}</errorDescription>\n"
        "    </UPnPError>\n"
        "   </detail>\n"
        "  </s:Fault>\n"
        " </s:Body>\n"
        "</s:Envelope>\n"
    )
    return body.encode("utf-8")


# ---------- action handlers ----------

def dispatch(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    """Route a parsed SOAP request to its handler. Raises SoapFault on unknown action."""
    key = (req.service_urn, req.action)
    handler = _HANDLERS.get(key)
    if handler is None:
        raise SoapFault(401, f"Invalid Action {req.action} on {req.service_urn}")
    return handler(req, ctx)


def _handle_get_max_num_profiles(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    return SoapResponse(args=[("NumProfilesAllowed", "1")])


def _handle_set_client_profile(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    profile_id = req.args.get("ProfileID", "0")
    client_profile = req.args.get("ClientProfile", "")
    ctx.profile_store.set(profile_id, client_profile)
    if ctx.session:
        ctx.session.event("client_profile", profile_id=profile_id, xml=client_profile)
        name = re.search(r"<modelName>([^<]*)</modelName>", client_profile)
        if name:
            ctx.session.note("car model", name.group(1))
    return SoapResponse(args=[("ResultProfile", client_profile)])


def _handle_get_client_profile(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    profile_id = req.args.get("ProfileID", "0")
    profile = ctx.profile_store.get(profile_id)
    return SoapResponse(args=[("ClientProfile", profile)])


def _handle_get_application_list(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    # We ignore AppListingFilter (always return everything). Per §4.5.2.2 a "*"
    # filter or empty string both mean "all elements".
    ctx.progress("applist")   # ends a handshake cycle: the next descriptor fetch rotates
    return SoapResponse(args=[("AppListing", render_app_listing(ctx, ctx.variant()))])


def _known_app(raw: str, ctx: ServerContext) -> int:
    """Validate an AppID against the current app list (810 malformed, 811 unknown)."""
    parsed = _parse_app_id(raw)
    if parsed is None:
        raise SoapFault(810, f"Bad AppID {raw!r}")
    listed = {int(a, 16) for a in advertised_app_ids(ctx.variant(), ctx.bt_address())}
    if parsed not in listed:
        raise SoapFault(811, f"Unauthorized AppID {raw!r}")
    return parsed


def _handle_launch_application(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    raw = req.args.get("AppID", "")
    parsed = _known_app(raw, ctx)
    variant = ctx.variant()
    if parsed == DAP_APP_ID_INT:
        # Part 4 §4.1.1.2: the DAP server listens at the returned URL.
        ctx.progress("dap_launch")
        if ctx.session:
            ctx.session.event("dap_launch", app_id=raw)
        ctx.app_status.set(parsed, "Foreground")
        changed = [DAP_APP_ID]
        app_uri = f"DAP://{ctx.address}:{ctx.dap_port}"
    elif parsed in BT_APP_IDS_INT:
        # Part 9 Table 4-7: the URI's host is the Bluetooth address; the car then sets
        # up the Bluetooth link to the phone (they're paired already).
        proto = BT_APP_IDS_INT[parsed]
        ctx.progress("bt_launch")
        if ctx.session:
            ctx.session.event("bt_launch", app_id=raw, protocol=proto)
        ctx.app_status.set(parsed, "Foreground")
        changed = [f"0x{parsed:08x}"]
        app_uri = f"{proto}://{ctx.bt_address()}"
    elif parsed in RTP_APP_IDS_INT:
        port = next(p for a, _pl, _d, p in RTP_APPS if int(a, 16) == parsed)
        ctx.progress("rtp_launch")
        if ctx.session:
            ctx.session.event("rtp_launch", app_id=raw, port=port)
        ctx.app_status.set(parsed, "Foreground")
        changed = [f"0x{parsed:08x}"]
        app_uri = f"RTP://{ctx.address}:{port}"
    else:
        if ctx.session:
            ctx.session.reach(STAGE_LAUNCH, app_id=raw)
        ctx.progress("launch")
        # §4.5.3.1: a launched UI app has the UI before the response. Our VNC server
        # runs continuously; when a UI app is launched the stand-alone VNC server's
        # status is reported too (Part 9 §4.5.3.1: "foreground or background").
        ctx.app_status.set(parsed, "Foreground")
        changed = [raw_id for raw_id in advertised_app_ids(variant, ctx.bt_address())
                   if int(raw_id, 16) == parsed]
        if parsed == HOME_APP_ID_INT:
            ctx.app_status.set(VNC_APP_ID_INT, "Foreground")
            changed.append(VNC_APP_ID)
        app_uri = f"{variant.uri_scheme}://{ctx.address}:{ctx.vnc_port}"
    # Part 9: AppStatusUpdate fires after the LaunchApplication response has been
    # sent (eventing delays the NOTIFY slightly for that).
    eventing.fire_event(
        ctx.subscription_store, _TM_APP_EVT_PATH,
        [("AppStatusUpdate", render_app_status_value(changed))], session=ctx.session,
    )
    return SoapResponse(args=[("AppURI", app_uri)])


def _handle_terminate_application(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    raw = req.args.get("AppID", "")
    parsed = _known_app(raw, ctx)
    ctx.progress("terminate")
    if ctx.session:
        ctx.session.event("terminate", app_id=raw)
    # Per §4.5.4: idempotent.
    ctx.app_status.set(parsed, "Notrunning")
    eventing.fire_event(
        ctx.subscription_store, _TM_APP_EVT_PATH,
        [("AppStatusUpdate", render_app_status_value([f"0x{parsed:08x}"]))],
        session=ctx.session,
    )
    return SoapResponse(args=[("TerminationResult", "true")])


def _handle_get_application_status(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    raw = req.args.get("AppID", VNC_APP_ID)
    if raw.strip() in ("*", ""):
        targets = [(int(a, 16), a)
                   for a in advertised_app_ids(ctx.variant(), ctx.bt_address())]
    else:
        parsed = _parse_app_id(raw)
        if parsed is None:
            raise SoapFault(810, f"Bad AppID {raw!r}")
        # Echo back the *exact* string the head unit sent (e.g. "0x1" not
        # "0x00000001"). Spec §4.2.5 says comparison must be by integer value
        # but VW MIB II calls Launch with "0x00000001" then Status with "0x1";
        # it might compare strings on its side.
        targets = [(parsed, raw)]

    entries = []
    for parsed_id, shown in targets:
        status_type = ctx.app_status.get(parsed_id)
        entries.append(
            '<appStatus>'
            f'<appID>{_xml_escape(shown)}</appID>'
            '<status>'
            '<profileID>0</profileID>'
            f'<statusType>{status_type}</statusType>'
            '</status>'
            '</appStatus>'
        )
    status_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<appStatusList xmlns="urn:schemas-upnp-org:tmapplicationserver:appstatus-1-0">'
        + "".join(entries)
        + '</appStatusList>'
    )
    return SoapResponse(args=[("AppStatus", status_xml)])


def _handle_get_application_certificate_info(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    # Filter is "*" or empty in practice; we only have one app, so just return it.
    return SoapResponse(args=[("AppCertificateList", render_app_certificate(ctx))])


_TM_APP = "urn:schemas-upnp-org:service:TmApplicationServer:1"
_TM_PROF = "urn:schemas-upnp-org:service:TmClientProfile:1"

_HANDLERS = {
    (_TM_PROF, "GetMaxNumProfiles"): _handle_get_max_num_profiles,
    (_TM_PROF, "SetClientProfile"): _handle_set_client_profile,
    (_TM_PROF, "GetClientProfile"): _handle_get_client_profile,
    (_TM_APP, "GetApplicationList"): _handle_get_application_list,
    (_TM_APP, "LaunchApplication"): _handle_launch_application,
    (_TM_APP, "TerminateApplication"): _handle_terminate_application,
    (_TM_APP, "GetApplicationStatus"): _handle_get_application_status,
    (_TM_APP, "GetApplicationCertificateInfo"): _handle_get_application_certificate_info,
}
