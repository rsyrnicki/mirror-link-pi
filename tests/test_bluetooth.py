"""Bluetooth audio entries (s6-audio-home-bt) and the phone's address."""

from __future__ import annotations

from mlpi import btaddr, soap
from mlpi.config import Config
from mlpi.http_descriptor import render_descriptor
from mlpi.variants import Variant, VariantManager, load_variants

ADDR = "A1B2C3D4E5F6"


def _ctx(bt=ADDR):
    return soap.ServerContext(address="192.168.7.2", http_port=8080, vnc_port=5900,
                              app_name="MirrorLink Pi", bt_address=lambda: bt)


def test_address_parsing():
    assert btaddr.parse_phone_output("a1:b2:c3:d4:e5:f6\n") == ADDR
    dumpsys = """Bluetooth Status
  enabled: true
  state: ON
  address: A1:B2:C3:D4:E5:F6
  name: Galaxy A56
Bonded devices:
  00:11:22:33:44:55 [BR/EDR] VW Polo
"""
    assert btaddr.parse_phone_output(dumpsys) == ADDR
    assert btaddr.parse_phone_output("null") == ""
    assert btaddr.parse_phone_output("02:00:00:00:00:00") == ""     # Android's dummy
    assert btaddr.parse_phone_output("Bonded devices:\n  00:11:22:33:44:55 car") == ""


def test_address_is_remembered_and_config_wins(tmp_path):
    cfg = Config()
    cfg.session.root = str(tmp_path)
    assert btaddr.current(cfg) == ""
    assert btaddr.remember(tmp_path, "a1:b2:c3:d4:e5:f6")
    assert not btaddr.remember(tmp_path, ADDR)                    # unchanged
    assert btaddr.current(cfg) == ADDR
    cfg.phone.bt_address = "11:22:33:44:55:66"
    assert btaddr.current(cfg) == "112233445566"


def test_bt_entries_in_app_list_and_launch():
    variant = next(v for v in load_variants() if v.name == "s6-audio-home-bt")
    ctx = _ctx()
    ctx.variant = lambda: variant
    listing = soap.render_app_listing(ctx, variant)
    assert "<protocolID>BTA2DP</protocolID><direction>out</direction>" in listing
    assert "<protocolID>BTHFP</protocolID><direction>bi</direction>" in listing
    assert "0x0000000A" in soap.advertised_app_ids(variant, ADDR)
    req = soap.SoapRequest(soap._TM_APP, "LaunchApplication",
                           {"AppID": "0x00000009", "ProfileID": "0"})
    resp = soap._handle_launch_application(req, ctx)
    assert resp.args == [("AppURI", f"BTA2DP://{ADDR}")]


def test_no_address_means_plain_s6_audio_home():
    variants = {v.name: v for v in load_variants()}
    bt, plain = variants["s6-audio-home-bt"], variants["s6-audio-home"]
    ctx = _ctx(bt="")
    assert soap.render_app_listing(ctx, bt) == soap.render_app_listing(ctx, plain)
    assert soap.advertised_app_ids(bt, "") == soap.advertised_app_ids(plain, "")


def test_descriptor_announces_bluetooth(tmp_path):
    cfg = Config()
    cfg.session.root = str(tmp_path)
    variant = Variant(name="bt", bt_apps=True)
    assert "X_connectivity" not in render_descriptor(cfg, "192.168.7.2", variant=variant)
    btaddr.remember(tmp_path, ADDR)
    xml = render_descriptor(cfg, "192.168.7.2", variant=variant)
    assert f"<bdAddr>{ADDR}</bdAddr><startConnection>false</startConnection>" in xml
    assert "X_connectivity" not in render_descriptor(cfg, "192.168.7.2", variant=Variant())


def test_start_variant_overrides_the_winner(tmp_path):
    (tmp_path / "winner-variant").write_text("b\n")
    vs = [Variant(name="a"), Variant(name="b"), Variant(name="c")]
    assert VariantManager(vs, state_dir=tmp_path).current.name == "b"
    assert VariantManager(vs, state_dir=tmp_path, start_variant="a").current.name == "a"


def test_bluetooth_variant_is_opt_in_only(tmp_path):
    """The MIB2 Standard refused the real phone over Bluetooth with the BT variant:
    the rotation must not land on it, and a saved winner must not bring it back."""
    vs = [Variant(name="plain"), Variant(name="bt", bt_apps=True), Variant(name="other")]
    (tmp_path / "winner-variant").write_text("bt\n")
    mgr = VariantManager(vs, state_dir=tmp_path)
    assert mgr.names() == ["plain", "other"] and mgr.current.name == "plain"
    opted = VariantManager(vs, state_dir=tmp_path, start_variant="bt")
    assert opted.current.name == "bt"
    assert VariantManager(vs, mode="fixed", fixed_variant="bt").current.name == "bt"
