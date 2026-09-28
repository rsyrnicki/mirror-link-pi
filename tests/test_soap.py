from __future__ import annotations

import pytest

from mlpi import soap
from mlpi.variants import Variant

REAL_SET_CLIENT_PROFILE = b'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:SetClientProfile xmlns:u="urn:schemas-upnp-org:service:TmClientProfile:1"><ProfileID>0</ProfileID><ClientProfile>&lt;clientProfile&gt;&lt;clientID&gt;VWAG_VOLKSWAGEN&lt;/clientID&gt;&lt;manufacturer&gt;VWAG_VOLKSWAGEN&lt;/manufacturer&gt;&lt;modelName&gt;VW-Mibstd2&lt;/modelName&gt;&lt;/clientProfile&gt;</ClientProfile></u:SetClientProfile></s:Body></s:Envelope>'''  # noqa: E501 - verbatim car capture

REAL_GET_CLIENT_PROFILE = b'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:GetClientProfile xmlns:u="urn:schemas-upnp-org:service:TmClientProfile:1"><ProfileID>0</ProfileID></u:GetClientProfile></s:Body></s:Envelope>'''  # noqa: E501 - verbatim car capture

REAL_GET_APP_LIST = b'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:GetApplicationList xmlns:u="urn:schemas-upnp-org:service:TmApplicationServer:1"><AppListingFilter>*</AppListingFilter><ProfileID>0</ProfileID></u:GetApplicationList></s:Body></s:Envelope>'''  # noqa: E501 - verbatim car capture

SET_PROFILE_ACTION = '"urn:schemas-upnp-org:service:TmClientProfile:1#SetClientProfile"'
GET_PROFILE_ACTION = '"urn:schemas-upnp-org:service:TmClientProfile:1#GetClientProfile"'
APP_LIST_ACTION = '"urn:schemas-upnp-org:service:TmApplicationServer:1#GetApplicationList"'


def _ctx(variant: Variant | None = None) -> soap.ServerContext:
    v = variant or Variant()
    return soap.ServerContext(address="192.168.7.2", http_port=8080, vnc_port=5900,
                              app_name="MirrorLink Pi Display", variant=lambda: v)


def _app(action: str, **args: str) -> soap.SoapRequest:
    return soap.SoapRequest("urn:schemas-upnp-org:service:TmApplicationServer:1", action, args)


def test_parse_real_set_client_profile_unescapes_once():
    """Captured live from VW MIB II head unit on 2026-05-01."""
    req = soap.parse_soap(REAL_SET_CLIENT_PROFILE, SET_PROFILE_ACTION)
    assert req.service_urn == "urn:schemas-upnp-org:service:TmClientProfile:1"
    assert req.action == "SetClientProfile"
    assert req.args["ProfileID"] == "0"
    assert req.args["ClientProfile"].startswith("<clientProfile><clientID>VWAG_VOLKSWAGEN")


def test_parse_real_get_client_profile():
    req = soap.parse_soap(REAL_GET_CLIENT_PROFILE, GET_PROFILE_ACTION)
    assert req.action == "GetClientProfile"
    assert req.args["ProfileID"] == "0"


def test_parse_real_get_application_list():
    req = soap.parse_soap(REAL_GET_APP_LIST, APP_LIST_ACTION)
    assert req.service_urn == "urn:schemas-upnp-org:service:TmApplicationServer:1"
    assert req.args["AppListingFilter"] == "*"
    assert req.args["ProfileID"] == "0"


def test_set_client_profile_is_escaped_exactly_once():
    """Regression: session 3 sent `&amp;lt;clientProfile` back to the car."""
    ctx = _ctx()
    set_req = soap.parse_soap(REAL_SET_CLIENT_PROFILE, SET_PROFILE_ACTION)
    body = soap.render_response(set_req, soap.dispatch(set_req, ctx)).decode()
    assert "<ResultProfile>&lt;clientProfile&gt;&lt;clientID&gt;VWAG_VOLKSWAGEN" in body
    assert "&amp;lt;" not in body

    get_req = soap.parse_soap(REAL_GET_CLIENT_PROFILE, GET_PROFILE_ACTION)
    body = soap.render_response(get_req, soap.dispatch(get_req, ctx)).decode()
    assert "<ClientProfile>&lt;clientProfile&gt;&lt;clientID&gt;VWAG_VOLKSWAGEN" in body
    assert "&amp;lt;" not in body


def test_nested_raw_xml_argument_is_kept():
    body = REAL_SET_CLIENT_PROFILE.replace(b"&lt;", b"<").replace(b"&gt;", b">")
    req = soap.parse_soap(body, SET_PROFILE_ACTION)
    assert req.args["ClientProfile"].startswith("<clientProfile><clientID>VWAG")


def test_dispatch_get_max_num_profiles():
    req = soap.SoapRequest("urn:schemas-upnp-org:service:TmClientProfile:1",
                           "GetMaxNumProfiles", {})
    resp = soap.dispatch(req, _ctx())
    assert resp.args == [("NumProfilesAllowed", "1")]


def test_app_listing_baseline_matches_session3_shape():
    req = soap.parse_soap(REAL_GET_APP_LIST, APP_LIST_ACTION)
    body = soap.render_response(req, soap.dispatch(req, _ctx())).decode()
    assert "<u:GetApplicationListResponse" in body
    listing = soap.render_app_listing(_ctx(), Variant())
    for part in ("<protocolID>VNC</protocolID>", "<trustLevel>0x0080</trustLevel>",
                 "<audioInfo>", "<appCertificateURL>", "<resourceStatus>free"):
        assert part in listing
    import xml.etree.ElementTree as ET
    ET.fromstring(listing.split("?>", 1)[1])  # well-formed


def test_app_listing_minimal_variant_drops_optional_claims():
    v = Variant(name="minimal", audio_info=False, app_trust_level="", audio_trust_level="",
                cert_url=False, display_content_category="0x00000000")
    listing = soap.render_app_listing(_ctx(v), v)
    assert "trustLevel" not in listing
    assert "audioInfo" not in listing
    assert "appCertificateURL" not in listing
    assert "<displayInfo><contentCategory>0x00000000</contentCategory></displayInfo>" in listing


def test_launch_sets_foreground_and_uses_variant_scheme():
    v = Variant(uri_scheme="VNC")
    ctx = _ctx(v)
    resp = soap.dispatch(_app("LaunchApplication", AppID="0x00000001", ProfileID="0"), ctx)
    assert resp.args == [("AppURI", "VNC://192.168.7.2:5900")]
    status = soap.dispatch(_app("GetApplicationStatus", AppID="0x1"), ctx)
    assert "<appID>0x1</appID>" in status.args[0][1]
    assert "Foreground" in status.args[0][1]
    listing = soap.render_app_listing(ctx, v)
    assert "<resourceStatus>busy" in listing


def test_launch_rejects_bad_and_unknown_app_ids():
    with pytest.raises(soap.SoapFault) as exc:
        soap.dispatch(_app("LaunchApplication", AppID="1"), _ctx())
    assert exc.value.code == 810
    with pytest.raises(soap.SoapFault) as exc:
        soap.dispatch(_app("LaunchApplication", AppID="0x2"), _ctx())
    assert exc.value.code == 811


def test_unknown_action_raises_soap_fault():
    req = soap.SoapRequest("urn:schemas-upnp-org:service:Bogus:1", "Nonsense", {})
    with pytest.raises(soap.SoapFault) as exc_info:
        soap.dispatch(req, _ctx())
    assert exc_info.value.code == 401
