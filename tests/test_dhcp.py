from __future__ import annotations

import socket
import struct

from mlpi import dhcp


def _request(msg_type: int, *, mac=b"\x64\xd4\xbd\xd2\x12\x5c", xid=0x55005926,
             requested: str | None = None, server_id: str | None = None,
             flags: int = 0) -> bytes:
    header = struct.pack("!BBBBIHH4s4s4s4s16s64s128s", 1, 1, 6, 0, xid, 0, flags,
                         b"\0" * 4, b"\0" * 4, b"\0" * 4, b"\0" * 4, mac.ljust(16, b"\0"),
                         b"", b"")
    opts = bytearray(dhcp.MAGIC_COOKIE)
    opts += bytes([53, 1, msg_type])
    if requested:
        opts += bytes([50, 4]) + socket.inet_aton(requested)
    if server_id:
        opts += bytes([54, 4]) + socket.inet_aton(server_id)
    opts += bytes([12, 4]) + b"MIB2"
    opts += bytes([55, 3, 1, 3, 6])
    opts.append(255)
    return header + bytes(opts)


def _server() -> dhcp.DhcpServer:
    return dhcp.DhcpServer(interface="usb0", server_ip="192.168.7.2", prefix=24,
                           client_ip="192.168.7.44")


def test_parse_request():
    pkt = dhcp.parse_packet(_request(dhcp.DISCOVER))
    assert pkt.msg_type == dhcp.DISCOVER
    assert pkt.mac == "64:d4:bd:d2:12:5c"
    info = dhcp.describe_options(pkt.options)
    assert info["msg_type"] == "DISCOVER"
    assert info["hostname"] == "MIB2"
    assert info["param_request_list"] == [1, 3, 6]


def test_discover_request_ack_flow():
    server = _server()
    mtype, ip, offer = server.reply_for(dhcp.parse_packet(_request(dhcp.DISCOVER)))
    assert (mtype, ip) == (dhcp.OFFER, "192.168.7.44")
    parsed = dhcp.parse_packet(offer)
    assert parsed.op == 2
    assert parsed.yiaddr == "192.168.7.44"
    assert parsed.msg_type == dhcp.OFFER
    assert socket.inet_ntoa(parsed.options[dhcp.OPT_SUBNET]) == "255.255.255.0"
    assert socket.inet_ntoa(parsed.options[dhcp.OPT_ROUTER]) == "192.168.7.2"
    assert socket.inet_ntoa(parsed.options[dhcp.OPT_SERVER_ID]) == "192.168.7.2"
    assert len(offer) >= 300

    mtype, ip, ack = server.reply_for(dhcp.parse_packet(
        _request(dhcp.REQUEST, requested="192.168.7.44", server_id="192.168.7.2")))
    assert (mtype, ip) == (dhcp.ACK, "192.168.7.44")
    assert struct.unpack("!I", dhcp.parse_packet(ack).options[dhcp.OPT_LEASE])[0] == 3600


def test_request_for_foreign_address_gets_nak():
    server = _server()
    mtype, _, _ = server.reply_for(dhcp.parse_packet(
        _request(dhcp.REQUEST, requested="10.0.0.5")))
    assert mtype == dhcp.NAK


def test_request_for_other_server_is_ignored():
    server = _server()
    assert server.reply_for(dhcp.parse_packet(
        _request(dhcp.REQUEST, requested="192.168.7.44", server_id="192.168.7.99"))) is None


def test_second_client_gets_next_address():
    server = _server()
    server.reply_for(dhcp.parse_packet(_request(dhcp.DISCOVER)))
    _, ip, _ = server.reply_for(dhcp.parse_packet(_request(dhcp.DISCOVER, mac=b"\x02" * 6)))
    assert ip == "192.168.7.45"


def test_broadcast_flag_means_broadcast_reply():
    server = _server()
    pkt = dhcp.parse_packet(_request(dhcp.DISCOVER, flags=0x8000))
    assert server._destination(pkt, dhcp.OFFER, "192.168.7.44") == "255.255.255.255"
