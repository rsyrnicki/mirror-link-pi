# How the Pi and the car communicate

MirrorLink connects a phone to a car's head unit over USB: the phone runs the apps and
the head unit displays them and sends touch input back. In MirrorLink terms the phone
is the *MirrorLink Server* and the head unit is the *MirrorLink Client*.

MirrorLink-Pi makes a Raspberry Pi Zero 2 W act as that server. In phone mode the Pi
in turn receives the screen of an Android phone and forwards it to the car.

```
Phone ──Wi-Fi──▶ Pi (MirrorLink server) ──USB──▶ Car head unit (MirrorLink client)
      ◀─touch──                         ◀─touch─
```

The connection is set up in stages, each depending on the previous one. The Pi's
green LED shows the furthest stage reached (see [`field-test.md`](field-test.md)).

## Stage 1: Network over USB (LED: 1 blink)

The Pi presents itself to the car as a USB network adapter. From then on, the USB
cable carries ordinary network traffic between the two devices.

- **USB gadget:** a Linux device acting in the USB *device* role rather than the host
  role. The Pi Zero's USB controller supports this; most computers' don't.
- **NCM** (Network Control Model): the USB device class used for network adapters.

On the Pi the resulting network interface is **`usb0`**.

## Stage 2: Addresses (LED: 2 blinks)

Both sides need an IP address before they can exchange anything else.

- **IP address:** the Pi uses `192.168.7.2`, the car receives `192.168.7.44`.
- **DHCP** (Dynamic Host Configuration Protocol): the car requests an address and the
  Pi's DHCP server assigns it.

## Stage 3: Discovery (LED: 3 blinks)

The car looks for a MirrorLink server on the new network.

- **SSDP** (Simple Service Discovery Protocol): the car sends a search request to a
  multicast address; the Pi replies with the URL of its device description.
- **UPnP** (Universal Plug and Play): the protocol family SSDP belongs to. MirrorLink
  builds its control layer on it.
- **Device descriptor:** an **XML** document in which the Pi describes itself: device
  type (MirrorLink server), name, and the services it offers.
- **HTTP:** the car downloads the descriptor from the Pi's web server (port 8080).

## Stage 4: App list and launch (LED: 4 blinks)

The car then calls the Pi's services:

1. **Client profile:** the car sends its own description (model `VW-Mibstd2`, supported
   audio formats, Bluetooth details).
2. **Application list:** the car asks which apps are available; the Pi returns its
   **app list**.
3. **Launch:** the car starts an app. The Pi replies with the address of the screen
   connection.

- **SOAP** (Simple Object Access Protocol): the format of these calls, XML messages over
  HTTP, e.g. *GetApplicationList* or *LaunchApplication*.
- **GENA** (General Event Notification Architecture): event messages from the Pi to the
  car, e.g. "the app is now running".

This stage was the main obstacle in the project. For a long time the car stopped
right after reading the app list. It only accepts a list that matches what real
phones send: once the Pi's list copied the structure of a Samsung Galaxy S6's (one
screen app plus RTP audio entries), the car launched it. That answer is the **variant**
`s6-audio-home`. A *variant* is one way of answering the car; the Pi can try several in
turn and remembers the one that worked.

## Stage 5: The screen (LED: solid)

The car opens the screen connection (port 5900).

- **VNC** (Virtual Network Computing): a protocol for transmitting a screen's contents
  and receiving mouse/touch and keyboard input.
- **RFB** (Remote Framebuffer): the wire protocol VNC uses. A **framebuffer** is the
  image of the whole screen in memory.

The sequence:
1. **Handshake:** both sides agree on the screen size (**800×480 pixels**) and the pixel
   format. The car uses **RGB565**: 16 bits per pixel, 5 for red, 6 for green, 5 for blue.
2. The car requests the screen; the Pi sends the full image and from then on only the
   regions that change (**updates**).
3. A touch on the car screen arrives at the Pi as a **pointer event** (position plus
   pressed/released); keys arrive as **key events**. Which keys a head unit sends is
   its choice: the MIB2 Standard sends only the text from its on-screen keyboard, no
   knob or hardware keys.

MirrorLink adds its own messages on top of VNC:
- **Context information:** the Pi tells the car what kind of content is shown (e.g. a
  home screen) and how trusted it is.
- **Framebuffer blocking:** the car tells the Pi it is hiding the picture, for example
  while driving with uncertified content ("The mobile device is restricted").
- **Device status:** small status messages such as day/night or driving mode.

**Certification:** real MirrorLink phones carry a digital certificate from the **CCC**
(Car Connectivity Consortium, the organisation behind MirrorLink), which the car can
verify via **DAP** (Device Attestation Protocol). The Pi has no such certificate, so a
stock head unit blocks its picture while the car is moving.

## Phone mode: getting the phone's screen

The Pi does not run Maps itself; it mirrors the phone.

1. **Wi-Fi hotspot:** the Pi runs its own Wi-Fi network, `MirrorLink-Pi`, which the
   phone joins. The hotspot offers no internet, so the phone keeps using **mobile data**.
2. **adb** (Android Debug Bridge): Android's developer interface for controlling a phone
   from a computer. **Wireless debugging** is adb over Wi-Fi. The phone only accepts the
   Pi because it was **paired** once (with a code typed on the car screen, or
   `mlpi pair-phone` on the laptop): the Pi holds a key the phone trusts.
3. **scrcpy** ("screen copy"): an open-source tool. Through adb, the Pi starts its small
   server program on the phone, which
   - creates a **virtual display**, an additional 800×480 screen inside the phone where
     Maps or Spotify run;
   - encodes that display as video and streams it to the Pi;
   - receives touch and key input from the Pi and injects it on the phone.
4. **H.264:** the video compression format of the stream. The Pi **decodes** it back into
   pixels with **libavcodec**, the decoder library from the **FFmpeg** project.
5. The Pi converts the pixels to RGB565 and sends them to the car over VNC (stage 5).
6. **Input path:** a touch on the car screen goes car → Pi (VNC pointer event) → phone
   (scrcpy touch message).

**Finding the phone:** the phone announces its debugging port with **mDNS** (multicast
DNS, service announcements on the local network). Android stops answering these while
its screen is off, so the Pi also probes the port range directly and remembers the last
working port.

**Audio** is not part of this path: it goes from the phone to the car over Bluetooth as
usual.

## Overview

```
Phone                    Pi Zero 2 W                          Car (MIB2)
─────                    ───────────                          ──────────
Maps, Spotify            USB gadget (NCM)  ◀─────USB─────▶   sees a network adapter
  on a virtual           DHCP: assigns .44                     requests an address
  800×480 display        SSDP: "MirrorLink server here"        searches the network
scrcpy: H.264 ──Wi-Fi──▶ HTTP/SOAP: descriptor, app list ◀─▶  reads, launches the app
       ◀── touches ───── decode → RGB565 → VNC  ◀────────▶    displays, sends
                                                               touches and keyboard text
Audio ───────────────────── Bluetooth ───────────────────▶    speakers
```

## Glossary

| Abbreviation | Stands for | Meaning here |
|---|---|---|
| USB | Universal Serial Bus | The physical link between Pi and car |
| NCM | Network Control Model | USB device class for network adapters |
| IP | Internet Protocol | Addressing on the USB network |
| DHCP | Dynamic Host Configuration Protocol | Assigns the car its IP address |
| UPnP | Universal Plug and Play | Protocol family for device discovery and control |
| SSDP | Simple Service Discovery Protocol | The car's search for a MirrorLink server |
| HTTP | Hypertext Transfer Protocol | Transport for the descriptor and SOAP calls |
| XML | Extensible Markup Language | Format of the descriptor, app list and SOAP messages |
| SOAP | Simple Object Access Protocol | Requests such as *LaunchApplication* |
| GENA | General Event Notification Architecture | Event messages from the Pi to the car |
| VNC / RFB | Virtual Network Computing / Remote Framebuffer | Screen transfer and input |
| RGB565 | Red-Green-Blue, 5/6/5 bits | 16-bit pixel format used by the car |
| RTP | Real-time Transport Protocol | Audio streaming in MirrorLink (listed, not used) |
| CCC | Car Connectivity Consortium | The organisation behind MirrorLink |
| DAP | Device Attestation Protocol | Verifies that a phone is certified |
| adb | Android Debug Bridge | Remote control interface for Android |
| scrcpy | screen copy | Mirrors and controls an Android screen |
| H.264 | (ITU-T video standard) | Video compression of the phone stream |
| FFmpeg / libavcodec | — | Video library the Pi decodes with |
| mDNS | Multicast Domain Name System | Service announcements on the local network |

The protocol details, clause by clause, are in [`spec-notes.md`](spec-notes.md).
