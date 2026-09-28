from __future__ import annotations

from mlpi import http_descriptor
from mlpi.config import Config


def test_render_descriptor_substitutes_all_placeholders():
    cfg = Config()
    cfg.network.http_port = 8080
    xml = http_descriptor.render_descriptor(cfg, "192.168.7.2")
    # All template placeholders must have been substituted.
    assert "${" not in xml
    # Sanity: declared device type and UUID land in the output.
    assert "TmServerDevice:1" in xml
    assert "uuid:c8cba096-5abe-47ac-9c14-3267d7c94ce6" in xml
    assert "<URLBase>http://192.168.7.2:8080/</URLBase>" in xml
    assert "/scpd/TmApplicationServer.xml" in xml
    assert "/scpd/TmClientProfile.xml" in xml
    assert "/scpd/TmNotificationServer.xml" in xml
    # Per ETSI TS 103 544-12 §7.6: omitting <X_mirrorLinkVersion> forces 1.0
    # fallback, which doesn't require DAP / X_Signature. Make sure we keep the
    # element omitted (the string may appear in a comment, that's fine).
    assert "<X_mirrorLinkVersion" not in xml
    # The fabricated services from earlier scaffolding must be gone.
    assert "TmServerStateMachineProfile" not in xml
    assert "TmProfileService" not in xml


def test_render_descriptor_picks_up_overrides():
    cfg = Config()
    cfg.device.friendly_name = "Test Device"
    cfg.device.model_name = "TestModel"
    cfg.network.http_port = 9000
    xml = http_descriptor.render_descriptor(cfg, "10.0.0.1")
    assert "<friendlyName>Test Device</friendlyName>" in xml
    assert "<modelName>TestModel</modelName>" in xml
    assert "http://10.0.0.1:9000/" in xml


def test_render_descriptor_escapes_xml_special_chars():
    """Free-text fields (manufacturer etc.) must not break XML when they contain &."""
    cfg = Config()
    cfg.device.manufacturer = "Sirnicki & Maksymilian"
    cfg.device.friendly_name = "<MirrorLink>"
    xml = http_descriptor.render_descriptor(cfg, "127.0.0.1")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)  # Raises on invalid XML.
    ns = {"u": "urn:schemas-upnp-org:device-1-0"}
    assert root.find("u:device/u:manufacturer", ns).text == "Sirnicki & Maksymilian"
    assert root.find("u:device/u:friendlyName", ns).text == "<MirrorLink>"


def test_all_referenced_scpds_exist():
    """Every SCPDURL in the template must point to an existing file."""
    cfg = Config()
    xml = http_descriptor.render_descriptor(cfg, "127.0.0.1")
    scpd_dir = http_descriptor.DEFAULT_SCPD_DIR
    import re
    for name in re.findall(r"/scpd/([\w.]+)", xml):
        assert (scpd_dir / name).is_file(), f"missing SCPD: {name}"
