# MirrorLink spec notes (ETSI TS 103 544 v1.3.1)

The MirrorLink specification is public: ETSI TS 103 544, 29 parts, v1.3.1 (2019-10),
the final release after the CCC wound down. `./scripts/fetch-spec.sh` downloads all
parts into `spec/` (not committed — ETSI copyright). This page lists what matters for
us, with clause references, and how the code follows it.

Relevant parts: 1 Connectivity · 2 VNC · 4 Device Attestation (DAP) · 9 UPnP
Application Server · 10 UPnP Client Profile · 12 UPnP Server Device · 13 Core
Architecture (session flow).

## Session flow (Part 13 §7)

1. USB: the client sends the **MirrorLink USB command**, CDC/NCM comes up, DHCP.
2. UPnP: SSDP, then the client fetches the device XML.
3. `SetClientProfile` — "defines the start of the MirrorLink session" (§7.3.3).
4. `GetApplicationList`, then **DAP** for clients ≥ 1.1 talking to a server ≥ 1.1
   (§7.3.4; may be deferred up to 1 min).
5. Launch the VNC server (or an app) → AppURI → TCP connect → RFB handshake.
6. Audio (RTP/Bluetooth) and CDB connections "at the start of the session" (§7.3.5).

The session version is the minimum of both sides (§7.6). A server **without**
`X_mirrorLinkVersion` is 1.0; a 1.0 session needs no DAP, but "limited or no
interoperability is possible with MirrorLink 1.0 devices" (§7.6 note).

## USB (Part 1)

| Clause | Requirement | Us |
|---|---|---|
| §4.2.2 | Client sends vendor request `bmRequestType 0x40, bRequest 0xF0, wValue = ML version (low byte major, high byte minor), wIndex = host VID, wLength 0`. Server enables CDC/NCM + SSDP. | Can be answered via a FunctionFS interface (`gadget.py`), **off by default** (`[usb] ml_command`) as it is hardware-fragile; version recorded in `usb.jsonl` when on. |
| §4.2.3 | A server that can't do MirrorLink STALLs it. A client detects a MirrorLink server by: command not STALLed **and** CDC/NCM **and** UPnP. | Before: the Linux gadget STALLed → the car may have classified us as "not a MirrorLink server". Now ACKed. Auto-fallback to NCM-only if the host dislikes the extra interface. |
| §5.2 | CDC/NCM, NTB-16. | `ncm` function. |
| §5.4.1 | Server has a DHCP server; addresses in 192.168.x.y with x = 2…127. | 192.168.7.2 / .44 ✓ |
| §5.4.2 | Don't use IPv6. | Not configured (kernel link-local only). |

## Device XML (Part 12)

| Clause | Requirement | Us |
|---|---|---|
| §4.3.1 table | `X_mirrorLinkVersion` **mandatory**; namespace `urn:schemas-carconnectivity-org:ml-1-1`. | Omitted for 1.0 variants (spec: missing = 1.0). When declared, now in the correct namespace — before, it had none, so a namespace-aware client never saw it. |
| §4.3.1 | `X_Signature` mandatory for ≥ 1.1, signed with the key attested via DAP; client "shall terminate" on validation failure. | Not implementable without a CCC-chained key. Omitted. |
| §4.3.1 | `bdAddr` if the server has Bluetooth. | Omitted (treated as "no Bluetooth radio"; avoids the client starting BT pairing). |
| §5 example | serviceId `urn:upnp-org:serviceId:TmApplicationServer1` etc. | Now matches. |

## Application list (Part 9)

| Clause | Requirement | Us |
|---|---|---|
| §5.2.1 | Stand-alone VNC server: `protocolID` VNC, **`appCategory` 0xF0000001** (Server functionality). | Default now. Every car session so far sent 0x00000000. |
| §5.2.5 | DAP endpoint: `protocolID` DAP, `appCategory` 0xF0000001, `format` = ML version. | Variants `*-dap`. |
| Annex A | 0x00010001 = Home screen; trust 0x0080 = registered application. | Variants `*-home` list a home-screen UI app. |
| §4.5.3.1 | "The MirrorLink UPnP Control Point may launch a VNC Server directly … the client should receive the server's current screen content." | That's what the car did (Launch 0x1). |
| Table 4-7 | AppURI scheme = protocol ID (`VNC://ip:port`, `DAP://ip:port`); case-insensitive. | `VNC://` default, `vnc://` in the legacy variant. |
| §4.2.7 note | 1.0/1.1 servers may omit the AppList Signature. | Omitted. |
| §4.2.2 | AppStatusUpdate / AppListUpdate values are comma-separated AppID lists. | ✓ |

## DAP (Part 4)

- Launched via `LaunchApplication`; plain XML over TCP, no framing.
- A real answer needs a TPM-style quote signed by a device key whose certificate
  chains to the CCC root — impossible for us.
- On failure the client "shall not display any content in **drive mode**", "should
  terminate the session", otherwise treats everything as uncertified (§5).
- Implementation note: 1.1/1.2 clients may not run DAP, or only in drive mode.
- `dap.py` answers `result 1` ("attestation not available") and records the request
  (client version, trust root, requested components).

## VNC (Part 2)

| Clause | Requirement | Us |
|---|---|---|
| §6.2 | RFB 3.8 (also 3.7 for 1.0 peers), security type None. | ✓ |
| §6.3 | Pixel formats ARGB 888 and RGB 565 mandatory; start landscape; ≥ 800×480. | ✓ 800×480 |
| §7.1 | Extension messages: `U8 128, U8 ext-type, U16 length, payload`; unknown types read and ignored. | ✓ (before: guessed header, closed on unknown) |
| §7.3.1 | On SetEncodings with **-523**: send **ServerDisplayConfiguration** (ext 1, 12 bytes) immediately, then **ServerEventConfiguration** (ext 3, 28 bytes). Client answers ext 2 / ext 4. The client may only pick pixel formats we announced. | ✓ `mirrorlink_vnc.py` |
| §7.4 | Server enables knob 0 shift x/y, push, rotate z and Device_Backward; pointer events; event mapping bit = 1. | ✓ |
| §7.6 | DeviceStatusRequest (ext 12) → DeviceStatus (ext 11) within 1 s; follow the driver-distraction flag. | ✓ |
| §7.2 | ByeBye (ext 0) → answer ByeBye. | ✓ |
| §7.8 | FramebufferBlockingNotification (ext 16) tells *why* the client blocks us. | Decoded + logged loudly. |
| §8.3 | Context Information (-524) rectangle before framebuffer data in the first update, every non-incremental one and on change. | ✓ (app category/trust per variant) |
| §7.7 | Content attestation (ext 13/14) needs the DAP-attested key. | Not implemented (logged). |

## Not implemented (and why)

- **Signatures / attestation** (X_Signature, AppList Signature, DAP quote, content
  attestation): need a CCC-chained device key.
- **Audio** (RTP server/client, Bluetooth A2DP/HFP) and **CDB**: not needed to show
  a picture; the car's ClientProfile announces RTP payloads 98/99, so if the car
  insists on audio before VNC, an RTP entry is the next thing to add.
- **H.264 / HSML / WFD**: Raw encoding is mandatory and sufficient.
