"""SOAP-1.1 envelope handling for the MirrorLink UPnP control endpoints.

Implements the actions the head unit actually invokes (and a few it might) per
ETSI TS 103 544-9/-10. ProfileID is always "0" per spec.

  TmClientProfile:1#GetMaxNumProfiles      -> NumProfilesAllowed=1
  TmClientProfile:1#SetClientProfile       -> store + echo back as ResultProfile
  TmClientProfile:1#GetClientProfile       -> echo last stored ClientProfile
  TmApplicationServer:1#GetApplicationList -> AppListing with one VNC server entry
  TmApplicationServer:1#LaunchApplication  -> AppURI = vnc://<address>:<vnc_port>
  TmApplicationServer:1#TerminateApplication -> TerminationResult=true (idempotent)
  TmApplicationServer:1#GetApplicationStatus -> Notrunning (we don't track it yet)
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from html import escape as _xml_escape

from . import eventing

log = logging.getLogger(__name__)


# Single VNC application advertised. ID must be non-zero per Part 9 §4.2.5.
VNC_APP_ID = "0x00000001"
VNC_APP_ID_INT = int(VNC_APP_ID, 16)


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


def render_app_listing(ctx: "ServerContext", *, app_name: str) -> str:
    """Build an AppListing XML advertising one stand-alone VNC server.

    Strict-required fields per Part 9 Table 4-3 are: appID, name (in app),
    protocolID (in remotingInfo), plus iconList/icon (required for VNC apps per
    the same table). Everything else (appInfo, displayInfo, resourceStatus,
    trustLevel, Signature) is "Optional" according to the table; the §4.2.7
    implementation note also permits 1.0/1.1 servers to omit the Signature.

    We deliberately omit trustLevel here because asserting a trust level (e.g.
    0x0080) without a backing CCC-signed Signature appears to make the VW MIB II
    flash "Error: MirrorLink" when the user opens the menu (observed 2026-05-02).
    Keep the entry to bare structural requirements until we know more.
    """
    icon_url = f"http://{ctx.address}:{ctx.http_port}/icon/mlpi.png"
    cert_url = f"http://{ctx.address}:{ctx.http_port}/cert/mlpi.cert"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<appList xmlns="urn:schemas-upnp-org:tmapplicationserver:applist-1-0">'
        '<app>'
        f'<appID>{VNC_APP_ID}</appID>'
        f'<name>{_xml_escape(app_name)}</name>'
        '<iconList>'
        '<icon>'
        '<mimetype>image/png</mimetype>'
        '<width>128</width>'
        '<height>128</height>'
        '<depth>24</depth>'
        f'<url>{_xml_escape(icon_url)}</url>'
        '</icon>'
        '</iconList>'
        '<remotingInfo>'
        '<protocolID>VNC</protocolID>'
        '</remotingInfo>'
        f'<appCertificateURL>{_xml_escape(cert_url)}</appCertificateURL>'
        '<appInfo>'
        '<appCategory>0x00000000</appCategory>'
        '<trustLevel>0x0080</trustLevel>'
        '</appInfo>'
        '<audioInfo>'
        '<audioType>all</audioType>'
        '<contentCategory>0x80000000</contentCategory>'
        '<trustLevel>0x0080</trustLevel>'
        '</audioInfo>'
        # Dynamic resourceStatus: per Part 9, "free"=slot available, "busy"=in
        # use. After Launch, MIB II re-fetches the AppList in its retry loop;
        # if it sees Foreground from GetApplicationStatus AND resourceStatus=free
        # in the AppList, the two views contradict and the head unit's state
        # machine may stall waiting for resolution. Reflect the runtime status.
        + (
            '<resourceStatus>busy</resourceStatus>'
            if ctx.app_status.get(VNC_APP_ID_INT) in ("Foreground", "Background")
            else '<resourceStatus>free</resourceStatus>'
        )
        + '</app>'
        '</appList>'
    )


def render_app_certificate(ctx: "ServerContext") -> str:
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
    """Parsed SOAP request: service URN + action name + extracted arg dict."""
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
    """In-memory store for client profiles. Thread-safe (one lock for all writes)."""

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
    calls GetApplicationStatus, and only proceeds to open the VNC connection if it
    sees Foreground. So we have to flip the status to Foreground synchronously when
    LaunchApplication returns, even though we don't actually own the VNC server's
    lifecycle (it's a separate systemd unit running continuously).
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

    def all(self) -> dict[int, str]:
        with self._lock:
            return dict(self._status)


@dataclass
class ServerContext:
    """Runtime context shared with SOAP handlers.

    The address + ports are needed to build URLs that the head unit will hand out
    to itself (the icon URL inside AppListing, the AppURI returned by
    LaunchApplication). They must match the actual bind addresses, so we plumb them
    through from the HTTP server rather than hard-coding.
    """
    profile_store: ProfileStore
    app_status: AppStatusStore
    subscription_store: eventing.SubscriptionStore
    address: str
    http_port: int
    vnc_port: int
    app_name: str


_TM_APP_EVT_PATH = "/evt/TmApplicationServer"


def render_app_status_value(canonical_app_id: str, status_type: str) -> str:
    """Build the AppStatusUpdate event value.

    Per Part 9 §4.2.2: this is a UTF-8 string holding a *comma-separated list*
    of A_ARG_TYPE_AppID values whose status has changed — NOT the full
    AppStatus XML block. The head unit, on receiving the event, calls
    GetApplicationStatus on the listed appIDs to fetch detail. Sending the
    XML body inline (as we did initially) was protocol-misaligned: head unit
    accepts the NOTIFY structurally (HTTP 200) but never acts on the value.

    `status_type` is unused but kept for call-site clarity / future extension.
    """
    del status_type
    return canonical_app_id


# ---------- parsing ----------

# Per SOAP spec the SOAPAction header is mandatory. Format:
#   "urn:schemas-upnp-org:service:Foo:1#ActionName"
_SOAP_ACTION_RE = re.compile(r'^"?(?P<urn>[^"#]+)#(?P<action>[^"]+)"?$')


def parse_soap(body: bytes, soap_action_header: str | None) -> SoapRequest:
    """Parse a SOAP envelope into action + args.

    Uses regex on the raw text rather than ``xml.etree`` because we want to preserve
    the exact text of nested arguments like ``ClientProfile`` (which contain entity-
    encoded XML). ``xml.etree`` would decode and re-emit them with different escaping,
    which the head unit then can't recognize when we echo them back.

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
        # Each arg is <ArgName>value</ArgName>. Values may contain entity-encoded XML.
        for arg_match in re.finditer(r"<(\w+)>(.*?)</\1>", inner, re.DOTALL):
            args[arg_match.group(1)] = arg_match.group(2)

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
        # Escape value but only for `&<>` since values may legitimately contain quotes.
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
    # ClientProfile arrives entity-encoded inside a single <ClientProfile> tag.
    client_profile_encoded = req.args.get("ClientProfile", "")
    # We store as-given (still entity-encoded); when we echo back we keep it that way.
    ctx.profile_store.set(profile_id, client_profile_encoded)
    return SoapResponse(args=[("ResultProfile", client_profile_encoded)])


def _handle_get_client_profile(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    profile_id = req.args.get("ProfileID", "0")
    profile = ctx.profile_store.get(profile_id)
    return SoapResponse(args=[("ClientProfile", profile)])


def _handle_get_application_list(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    # We ignore AppListingFilter (always return everything). Per §4.5.2.2 a "*"
    # filter or empty string both mean "all elements"; honouring more granular
    # filters is an optimization we don't need yet.
    return SoapResponse(args=[("AppListing", render_app_listing(ctx, app_name=ctx.app_name))])


def _handle_launch_application(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    raw = req.args.get("AppID", "")
    parsed = _parse_app_id(raw)
    if parsed is None:
        raise SoapFault(810, f"Bad AppID {raw!r}")
    if parsed != VNC_APP_ID_INT:
        raise SoapFault(811, f"Unauthorized AppID {raw!r}")
    # Per §4.5.3.1: "If the application being launched is a UI application, then
    # the MirrorLink Server device shall give control of the UI to the launched
    # application before returning a response." Our VNC server runs continuously
    # so it's already "in foreground" — flip the tracked status to match. The head
    # unit polls GetApplicationStatus right after Launch and won't open the VNC
    # connection unless it sees Foreground.
    ctx.app_status.set(parsed, "Foreground")
    # Spec Part 9: AppStatusUpdate event must fire AFTER the LaunchApplication
    # response has been sent. Without this NOTIFY, MIB II hangs at "Error: App"
    # — the polled GetApplicationStatus is not enough; the head unit only
    # opens the AppURI once it sees the eventing channel confirm Foreground.
    eventing.fire_event(
        ctx.subscription_store,
        _TM_APP_EVT_PATH,
        [("AppStatusUpdate", render_app_status_value(VNC_APP_ID, "Foreground"))],
    )
    app_uri = f"vnc://{ctx.address}:{ctx.vnc_port}"
    return SoapResponse(args=[("AppURI", app_uri)])


def _handle_terminate_application(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    raw = req.args.get("AppID", "")
    parsed = _parse_app_id(raw)
    if parsed is None:
        raise SoapFault(810, f"Bad AppID {raw!r}")
    if parsed != VNC_APP_ID_INT:
        raise SoapFault(811, f"Unauthorized AppID {raw!r}")
    # Per §4.5.4: idempotent. We don't actually own the VNC server lifetime — the
    # systemd unit on the Pi runs continuously — so claim termination succeeds.
    ctx.app_status.set(parsed, "Notrunning")
    eventing.fire_event(
        ctx.subscription_store,
        _TM_APP_EVT_PATH,
        [("AppStatusUpdate", render_app_status_value(VNC_APP_ID, "Notrunning"))],
    )
    return SoapResponse(args=[("TerminationResult", "true")])


def _handle_get_application_status(req: SoapRequest, ctx: ServerContext) -> SoapResponse:
    raw = req.args.get("AppID", VNC_APP_ID)
    if raw == "*":
        # Wildcard → return status for every known app, in our canonical form.
        targets = [(VNC_APP_ID_INT, VNC_APP_ID)]
    else:
        parsed = _parse_app_id(raw)
        if parsed is None:
            raise SoapFault(810, f"Bad AppID {raw!r}")
        # Echo back the *exact* string the head unit sent (e.g. "0x1" not
        # "0x00000001"). Spec §4.2.5 says comparison must be by integer value
        # but VW MIB II calls Launch with "0x00000001" then Status with "0x1"
        # and was observed bailing out when our response used the canonical
        # form — likely a string-equality check on its side.
        targets = [(parsed, raw)]

    entries = []
    for parsed_id, canonical in targets:
        status_type = ctx.app_status.get(parsed_id)
        entries.append(
            '<appStatus>'
            f'<appID>{_xml_escape(canonical)}</appID>'
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
