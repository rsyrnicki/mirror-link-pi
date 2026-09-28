from __future__ import annotations

import pytest

from mlpi import soap


REAL_SET_CLIENT_PROFILE = b'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:SetClientProfile xmlns:u="urn:schemas-upnp-org:service:TmClientProfile:1"><ProfileID>0</ProfileID><ClientProfile>&lt;clientProfile&gt;&lt;clientID&gt;VWAG_VOLKSWAGEN&lt;/clientID&gt;&lt;manufacturer&gt;VWAG_VOLKSWAGEN&lt;/manufacturer&gt;&lt;modelName&gt;VW-Mibstd2&lt;/modelName&gt;&lt;/clientProfile&gt;</ClientProfile></u:SetClientProfile></s:Body></s:Envelope>'''

REAL_GET_CLIENT_PROFILE = b'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:GetClientProfile xmlns:u="urn:schemas-upnp-org:service:TmClientProfile:1"><ProfileID>0</ProfileID></u:GetClientProfile></s:Body></s:Envelope>'''

REAL_GET_APP_LIST = b'''<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:GetApplicationList xmlns:u="urn:schemas-upnp-org:service:TmApplicationServer:1"><AppListingFilter>*</AppListingFilter><ProfileID>0</ProfileID></u:GetApplicationList></s:Body></s:Envelope>'''


def test_parse_real_set_client_profile():
    """Captured live from VW MIB II head unit on 2026-05-01."""
    soap_action = '"urn:schemas-upnp-org:service:TmClientProfile:1#SetClientProfile"'
    req = soap.parse_soap(REAL_SET_CLIENT_PROFILE, soap_action)
    assert req.service_urn == "urn:schemas-upnp-org:service:TmClientProfile:1"
    assert req.action == "SetClientProfile"
    assert req.args["ProfileID"] == "0"
    # ClientProfile is entity-encoded XML
    assert "VWAG_VOLKSWAGEN" in req.args["ClientProfile"]
    assert "VW-Mibstd2" in req.args["ClientProfile"]


def test_parse_real_get_client_profile():
    soap_action = '"urn:schemas-upnp-org:service:TmClientProfile:1#GetClientProfile"'
    req = soap.parse_soap(REAL_GET_CLIENT_PROFILE, soap_action)
    assert req.action == "GetClientProfile"
    assert req.args["ProfileID"] == "0"


def test_parse_real_get_application_list():
    soap_action = '"urn:schemas-upnp-org:service:TmApplicationServer:1#GetApplicationList"'
    req = soap.parse_soap(REAL_GET_APP_LIST, soap_action)
    assert req.service_urn == "urn:schemas-upnp-org:service:TmApplicationServer:1"
    assert req.action == "GetApplicationList"
    assert req.args["AppListingFilter"] == "*"
    assert req.args["ProfileID"] == "0"


def test_dispatch_set_then_get_round_trip():
    store = soap.ProfileStore()
    set_req = soap.parse_soap(
        REAL_SET_CLIENT_PROFILE,
        '"urn:schemas-upnp-org:service:TmClientProfile:1#SetClientProfile"',
    )
    set_resp = soap.dispatch(set_req, store)
    # SetClientProfile returns ResultProfile = what came in
    assert any(name == "ResultProfile" for name, _ in set_resp.args)
    body = soap.render_response(set_req, set_resp).decode()
    assert "<u:SetClientProfileResponse" in body
    assert "VWAG_VOLKSWAGEN" in body

    get_req = soap.parse_soap(
        REAL_GET_CLIENT_PROFILE,
        '"urn:schemas-upnp-org:service:TmClientProfile:1#GetClientProfile"',
    )
    get_resp = soap.dispatch(get_req, store)
    body = soap.render_response(get_req, get_resp).decode()
    assert "<u:GetClientProfileResponse" in body
    # Should echo back the previously stored profile
    assert "VWAG_VOLKSWAGEN" in body


def test_dispatch_get_max_num_profiles():
    req = soap.SoapRequest(
        service_urn="urn:schemas-upnp-org:service:TmClientProfile:1",
        action="GetMaxNumProfiles",
        args={},
    )
    resp = soap.dispatch(req, soap.ProfileStore())
    assert resp.args == [("NumProfilesAllowed", "1")]
    body = soap.render_response(req, resp).decode()
    assert "<NumProfilesAllowed>1</NumProfilesAllowed>" in body


def test_dispatch_get_application_list_returns_empty():
    req = soap.parse_soap(
        REAL_GET_APP_LIST,
        '"urn:schemas-upnp-org:service:TmApplicationServer:1#GetApplicationList"',
    )
    resp = soap.dispatch(req, soap.ProfileStore())
    assert any(name == "AppListing" for name, _ in resp.args)
    body = soap.render_response(req, resp).decode()
    assert "<u:GetApplicationListResponse" in body
    assert "appList" in body  # the empty <appList/> element


def test_unknown_action_raises_soap_fault():
    req = soap.SoapRequest(
        service_urn="urn:schemas-upnp-org:service:Bogus:1",
        action="Nonsense",
        args={},
    )
    with pytest.raises(soap.SoapFault) as exc_info:
        soap.dispatch(req, soap.ProfileStore())
    assert exc_info.value.code == 401
