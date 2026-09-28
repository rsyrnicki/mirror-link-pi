from __future__ import annotations

import base64
import json
import struct

from mlpi import dhcp
from mlpi.session import Session
from mlpi.tools import probe_phone as pp

REF_CERT = "captures/ccc-reference/self-signed.ccc.crt"


def test_header_and_arg_parsing():
    resp = "HTTP/1.1 200 OK\r\nLOCATION: http://192.168.7.44:49152/desc.xml\r\nST: x\r\n\r\n"
    assert pp._header(resp, "LOCATION") == "http://192.168.7.44:49152/desc.xml"
    assert pp._arg("<AppURI>vnc://1.2.3.4:5900</AppURI>", "AppURI") == "vnc://1.2.3.4:5900"
    assert pp._arg("<x>&lt;e&gt;</x>", "x") == "<e>"


def test_control_path_extraction():
    desc = ("<serviceList>"
            "<service><serviceType>urn:schemas-upnp-org:service:TmClientProfile:1</serviceType>"
            "<controlURL>/upnp/control/TmClientProfile</controlURL></service>"
            "<service><serviceType>urn:schemas-upnp-org:service:TmApplicationServer:1</serviceType>"
            "<controlURL>/upnp/control/TmApplicationServer</controlURL></service></serviceList>")
    assert pp._control_path(desc, "urn:schemas-upnp-org:service:TmClientProfile:1") \
        == "/upnp/control/TmClientProfile"
    assert pp._control_path(desc, "urn:schemas-upnp-org:service:TmApplicationServer:1") \
        == "/upnp/control/TmApplicationServer"


def test_dap_certificate_extraction(tmp_path):
    der = open(REF_CERT, "rb").read()
    b64 = base64.b64encode(der).decode()
    xml = ("<attestationResponse><version><majorVersion>1</majorVersion>"
           "<minorVersion>1</minorVersion></version><result>0</result>"
           f"<attestation><componentID>TerminalMode:VNC-Server</componentID></attestation>"
           f"<deviceCertificate>{b64}</deviceCertificate>"
           f"<manufacturerCertificate>{b64}</manufacturerCertificate></attestationResponse>")
    session = Session(tmp_path / "0001", boot_number=0)
    out = tmp_path / "dap"
    out.mkdir()
    pp._save_certificates(xml, session, out)
    session.close()
    assert (out / "device-0.der").read_bytes() == der
    assert (out / "manufacturer-1.der").read_bytes() == der
    certs = (out / "certs.txt").read_text()
    assert "notAfter=" in certs and "notBefore=" in certs   # openssl decoded the dates
    events = [json.loads(line) for line in (tmp_path / "0001/events.jsonl").open()]
    cert_events = [e for e in events if e["kind"] == "dap_certificate"]
    assert len(cert_events) == 2
    assert "notAfter" in cert_events[0]


def test_dhcp_client_packet_is_accepted_by_our_server():
    """The client's DISCOVER/REQUEST parse cleanly and the server answers them."""
    mac = b"\x02\x00\x00\x00\x00\x2c"
    discover = dhcp.parse_packet(dhcp._client_packet(dhcp.DISCOVER, 0x1234, mac))
    assert discover.msg_type == dhcp.DISCOVER and discover.mac == "02:00:00:00:00:2c"
    assert discover.wants_broadcast
    server = dhcp.DhcpServer(interface="usb0", server_ip="192.168.7.2", prefix=24,
                             client_ip="192.168.7.44")
    mtype, ip, offer = server.reply_for(discover)
    assert (mtype, ip) == (dhcp.OFFER, "192.168.7.44")
    req = dhcp.parse_packet(dhcp._client_packet(
        dhcp.REQUEST, 0x1234, mac, requested=ip, server_id="192.168.7.2"))
    mtype, ip, ack = server.reply_for(req)
    assert mtype == dhcp.ACK
    parsed = dhcp.parse_packet(ack)
    assert parsed.yiaddr == "192.168.7.44"
    assert struct.unpack("!I", parsed.options[dhcp.OPT_LEASE])[0] > 0
