"""MirrorLink VNC extension messages and pseudo encodings (ETSI TS 103 544-2).

All extension messages share RFB message type 128 (Part 2 §7.1, Table 4):

    U8 128 | U8 extension-type | U16 payload length | payload

Session start (§7.3.1, §7.4): when the client's SetEncodings contains the MirrorLink
pseudo encoding (-523), the server immediately sends ServerDisplayConfiguration and
then ServerEventConfiguration; the client answers each with its Client* counterpart.
Unknown extension types — and extra trailing bytes of known ones — are read and
ignored (§7.1).
"""

from __future__ import annotations

import struct

MSG_MIRRORLINK = 128

# Pseudo encodings (§8.1 Table 25)
ENC_MIRRORLINK = -523
ENC_CONTEXT_INFO = -524
ENC_DESKTOP_SIZE = -223

# Extension types (§7.1 Table 5)
EXT_BYEBYE = 0
EXT_SERVER_DISPLAY_CONFIG = 1
EXT_CLIENT_DISPLAY_CONFIG = 2
EXT_SERVER_EVENT_CONFIG = 3
EXT_CLIENT_EVENT_CONFIG = 4
EXT_EVENT_MAPPING = 5
EXT_EVENT_MAPPING_REQUEST = 6
EXT_DEVICE_STATUS = 11
EXT_DEVICE_STATUS_REQUEST = 12
EXT_CONTENT_ATTESTATION_RESPONSE = 13
EXT_CONTENT_ATTESTATION_REQUEST = 14
EXT_FB_BLOCKING_NOTIFICATION = 16
EXT_AUDIO_BLOCKING_NOTIFICATION = 18
EXT_TOUCH_EVENT = 20

EXT_NAMES = {
    0: "ByeBye", 1: "ServerDisplayConfiguration", 2: "ClientDisplayConfiguration",
    3: "ServerEventConfiguration", 4: "ClientEventConfiguration", 5: "EventMapping",
    6: "EventMappingRequest", 7: "KeyEventListing", 8: "KeyEventListingRequest",
    9: "VirtualKeyboardTrigger", 10: "VirtualKeyboardTriggerRequest", 11: "DeviceStatus",
    12: "DeviceStatusRequest", 13: "ContentAttestationResponse",
    14: "ContentAttestationRequest", 16: "FramebufferBlockingNotification",
    18: "AudioBlockingNotification", 20: "TouchEvent", 21: "FramebufferAlternativeText",
    22: "FramebufferAlternativeTextRequest",
}

# Pixel format support bits (§7.3.1 Table 7)
PF_ARGB888 = 1 << 0
PF_RGB565 = 1 << 16

# Knob 0 events a server shall pass on (§7.4): shift x, shift y, push z, rotate z.
KNOB0_REQUIRED = (1 << 0) | (1 << 1) | (1 << 3) | (1 << 7)
# Device keys: bit n <-> keysym 0x300002nn (Annex B Table B.2).
DEVICE_KEY_OK = 1 << 0x06
DEVICE_KEY_BACKWARD = 1 << 0x0C
DEVICE_KEY_HOME = 1 << 0x0D


def message(ext_type: int, payload: bytes = b"") -> bytes:
    return struct.pack("!BBH", MSG_MIRRORLINK, ext_type, len(payload)) + payload


def byebye() -> bytes:
    return message(EXT_BYEBYE)


def server_display_configuration(major: int, minor: int,
                                 pixel_formats: int = PF_ARGB888 | PF_RGB565) -> bytes:
    """Table 7. We offer no server-side scaling/rotation (all deprecated or optional);
    relative pixel size is 1:1 (deprecated fields, "shall be 1")."""
    payload = struct.pack("!BBHHHI", major, minor, 0, 1, 1, pixel_formats)
    assert len(payload) == 12
    return message(EXT_SERVER_DISPLAY_CONFIG, payload)


def _lang(code: str) -> int:
    raw = code.encode("ascii")[:2].ljust(2, b"\0")
    return struct.unpack("!H", raw)[0]


def server_event_configuration(*, keyboard_language: str = "en", keyboard_country: str = "US",
                               ui_language: str = "en", ui_country: str = "US") -> bytes:
    """Table 11: 8 bytes of ISO language/country codes + 5 × U32."""
    knob = KNOB0_REQUIRED
    device = DEVICE_KEY_OK | DEVICE_KEY_BACKWARD | DEVICE_KEY_HOME
    multimedia = 0
    key_related = 1 << 3            # event mapping support "Shall be '1'"; 0 function keys
    pointer_related = (1 << 0) | (0x01 << 8)  # pointer events, button 1; no touch events
    payload = struct.pack("!HHHHIIIII", _lang(keyboard_language), _lang(keyboard_country),
                          _lang(ui_language), _lang(ui_country),
                          knob, device, multimedia, key_related, pointer_related)
    assert len(payload) == 28
    return message(EXT_SERVER_EVENT_CONFIG, payload)


# DeviceStatus / DeviceStatusRequest feature field (Tables 15/16), 2-bit values.
_DS_FIELDS = {
    "key_lock": 0, "device_lock": 2, "screen_saver": 4, "night_mode": 6,
    "voice_input": 8, "mic_input": 10, "driver_distraction": 16,
}
DS_UNKNOWN, DS_RESERVED, DS_DISABLED, DS_ENABLED = 0, 1, 2, 3


def decode_device_status(value: int) -> dict[str, int]:
    out = {name: (value >> shift) & 3 for name, shift in _DS_FIELDS.items()}
    out["rotation"] = (value >> 24) & 7
    out["orientation"] = (value >> 27) & 3
    return out


def device_status(*, driver_distraction: int = DS_UNKNOWN) -> bytes:
    """Our status: not locked; driver distraction as last requested by the client;
    rotation 0° ("100") and landscape ("10") as mandated."""
    value = (DS_DISABLED << _DS_FIELDS["device_lock"]
             | driver_distraction << _DS_FIELDS["driver_distraction"]
             | 0b100 << 24 | 0b10 << 27)
    return message(EXT_DEVICE_STATUS, struct.pack("!I", value))


def decode_client_display_configuration(payload: bytes) -> dict[str, int]:
    """Table 9 (22 bytes; longer payloads from future versions are allowed)."""
    (major, minor, fb_conf, w_px, h_px, w_mm, h_mm, dist_mm, pix_fmt,
     resize) = struct.unpack_from("!BBHHHHHHII", payload.ljust(22, b"\0"))
    return {"major": major, "minor": minor, "framebuffer_config": fb_conf,
            "width_px": w_px, "height_px": h_px, "width_mm": w_mm, "height_mm": h_mm,
            "distance_mm": dist_mm, "pixel_formats": pix_fmt, "resize_factors": resize}


def decode_event_configuration(payload: bytes) -> dict[str, object]:
    """Tables 11/12 (identical layout for server and client)."""
    kl, kc, ul, uc, knob, device, multimedia, key_rel, ptr_rel = struct.unpack_from(
        "!HHHHIIIII", payload.ljust(28, b"\0"))

    def code(v: int) -> str:
        return struct.pack("!H", v).decode("latin-1").strip("\0")

    return {"keyboard": f"{code(kl)}-{code(kc)}", "ui": f"{code(ul)}-{code(uc)}",
            "knob_keys": f"0x{knob:08x}", "device_keys": f"0x{device:08x}",
            "multimedia_keys": f"0x{multimedia:08x}", "key_related": f"0x{key_rel:08x}",
            "function_keys": (key_rel >> 8) & 0xFF,
            "pointer_events": bool(ptr_rel & 1), "touch_events": bool(ptr_rel & 2),
            "button_mask": (ptr_rel >> 8) & 0xFF, "touch_points": ((ptr_rel >> 16) & 0xFF) + 1,
            "pressure_mask": (ptr_rel >> 24) & 0xFF}


def decode_touch_event(payload: bytes) -> list[dict[str, int]]:
    """TouchEvent (§7.10): U8 count, then count × (U16 x, U16 y, U8 id, U8 pressure)."""
    if not payload:
        return []
    count = payload[0]
    events = []
    for i in range(count):
        off = 1 + 6 * i
        if off + 6 > len(payload):
            break
        x, y, ident, pressure = struct.unpack_from("!HHBB", payload, off)
        events.append({"x": x, "y": y, "id": ident, "pressure": pressure})
    return events


_BLOCK_REASONS = {0: "content category", 1: "application category",
                  2: "content trust level", 3: "application trust level / certification",
                  4: "content rules", 5: "application ID", 8: "UI not in focus",
                  9: "UI not visible", 10: "UI layout (portrait)"}


def decode_blocking_notification(payload: bytes) -> dict[str, object]:
    """FramebufferBlockingNotification (§7.8 Table 20): the client tells us *why* it
    does not show our framebuffer. 1.0-1.2 clients may set several reason bits."""
    x, y, w, h, app_id, reasons = struct.unpack_from("!HHHHIH", payload.ljust(14, b"\0"))
    return {"rect": [x, y, w, h], "app_id": f"0x{app_id:08x}", "reason_bits": f"0x{reasons:04x}",
            "reasons": [text for bit, text in _BLOCK_REASONS.items() if reasons >> bit & 1]}


def context_information_rect(width: int, height: int, *, app_id: int, trust: int,
                             app_category: int, content_category: int = 0) -> bytes:
    """Context Information pseudo-encoding rectangle (§8.3 Table 26), covering the
    whole framebuffer. Must precede framebuffer data in the FramebufferUpdate."""
    return (struct.pack("!HHHHi", 0, 0, width, height, ENC_CONTEXT_INFO)
            + struct.pack("!IHHIII", app_id, trust, trust, app_category,
                          content_category, 0))
