#!/usr/bin/env python3
"""Replace identifiers (e.g. the car's Bluetooth address) in pcap files, in place.

Replacements must keep the length, so packets keep their sizes; IPv4, TCP and UDP
checksums of every changed packet are recomputed, so Wireshark still shows them as
valid. Matching is case-insensitive; the replacement keeps the original's case.

Usage:
  scripts/scrub-captures.py OLD=NEW [OLD=NEW ...] -- FILE.pcap [FILE.pcap ...]
  scripts/scrub-captures.py aabbccddeeff=020000001234 -- captures/*.pcap
"""

from __future__ import annotations

import re
import struct
import sys


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def fix_checksums(frame: bytearray) -> None:
    """Recompute IPv4 header + TCP/UDP checksums of an Ethernet II frame."""
    if len(frame) < 34 or frame[12:14] != b"\x08\x00":
        return
    ip = 14
    ihl = (frame[ip] & 0x0F) * 4
    total_len = struct.unpack_from("!H", frame, ip + 2)[0]
    end = min(len(frame), ip + total_len)
    frame[ip + 10:ip + 12] = b"\0\0"
    frame[ip + 10:ip + 12] = struct.pack("!H", _checksum(bytes(frame[ip:ip + ihl])))
    proto = frame[ip + 9]
    l4 = ip + ihl
    if proto not in (6, 17) or struct.unpack_from("!H", frame, ip + 6)[0] & 0x1FFF:
        return  # not TCP/UDP, or a non-first fragment
    offset = 16 if proto == 6 else 6
    if end < l4 + offset + 2:
        return  # truncated capture: leave as is
    if proto == 17 and frame[l4 + 6:l4 + 8] == b"\0\0":
        return  # UDP checksum not in use
    segment = bytearray(frame[l4:end])
    segment[offset:offset + 2] = b"\0\0"
    pseudo = frame[ip + 12:ip + 20] + struct.pack("!BBH", 0, proto, len(segment))
    value = _checksum(bytes(pseudo + segment))
    if proto == 17 and value == 0:
        value = 0xFFFF
    frame[l4 + offset:l4 + offset + 2] = struct.pack("!H", value)


def _replace(frame: bytes, pairs: list[tuple[bytes, bytes]]) -> tuple[bytes, int]:
    count = 0
    for old, new in pairs:
        def sub(m: re.Match, new: bytes = new) -> bytes:
            return new.upper() if m.group(0).isupper() else new

        frame, n = re.subn(re.escape(old), sub, frame, flags=re.IGNORECASE)
        count += n
    return frame, count


def scrub(path: str, pairs: list[tuple[bytes, bytes]]) -> int:
    data = open(path, "rb").read()
    magic = struct.unpack_from("<I", data)[0]
    endian = "<" if magic in (0xA1B2C3D4, 0xA1B23C4D) else ">"
    linktype = struct.unpack_from(endian + "I", data, 20)[0]
    out, pos, total = bytearray(data[:24]), 24, 0
    while pos + 16 <= len(data):
        hdr = data[pos:pos + 16]
        caplen = struct.unpack_from(endian + "I", hdr, 8)[0]
        frame, n = _replace(data[pos + 16:pos + 16 + caplen], pairs)
        if n:
            total += n
            frame = bytearray(frame)
            if linktype == 1:
                fix_checksums(frame)
        out += hdr + frame
        pos += 16 + caplen
    out += data[pos:]
    if total:
        open(path, "wb").write(out)
    return total


def main(argv: list[str]) -> int:
    if "--" not in argv:
        print(__doc__, file=sys.stderr)
        return 2
    split = argv.index("--")
    pairs = []
    for arg in argv[:split]:
        old, _, new = arg.partition("=")
        if len(old) != len(new) or not old:
            print(f"{arg}: OLD and NEW must have the same, non-zero length", file=sys.stderr)
            return 2
        pairs.append((old.encode(), new.encode()))
    for path in argv[split + 1:]:
        print(f"{path}: {scrub(path, pairs)} replacement(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
