import struct

from mlpi.capture import PcapWriter, pixel_filter


def test_pcap_writer_stops_at_total_cap(tmp_path):
    w = PcapWriter(tmp_path / "usb0.pcap", max_bytes=1000, max_total=2500)
    for _ in range(100):
        w.write(b"x" * 84)
    assert w.full
    assert w.packets == 25                          # 25 * (16 + 84) = 2500
    written = sum(p.stat().st_size for p in tmp_path.glob("usb0*.pcap"))
    assert written <= 2500 + 3 * 24                # plus one header per rotated file
    w.write(b"late")                                # ignored once full
    assert w.packets == 25


def test_pixel_filter_jumps_land_inside_the_program():
    prog = pixel_filter(5900)
    for i, (code, jt, jf, _k) in enumerate(prog):
        if code & 0x07 == 0x05:                     # BPF_JMP
            assert i + 1 + jt < len(prog) and i + 1 + jf < len(prog)
    assert prog[-1] == (0x06, 0, 0, 0) and prog[-2][0] == 0x06
    assert all(len(struct.pack("HBBI", *ins)) == 8 for ins in prog)
