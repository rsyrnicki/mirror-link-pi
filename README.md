# MirrorLink Pi
( This project is in the research state, there has been no results yet) 

The end goal is to host Android Auto on a Raspberry Pi and display it over MirrorLink. For now I do some research and try to establish a connection between my raspi and my car. Second step will be to show the Raspberry Pi OS desktop on the car's head unit.

## What we know so far:
MirrorLink uses multiple protocols to mirror a mobile device's screen in selected apps. It used to work with some older Android phones, and some even older Symbian devices.

First protocols I have to tackle are USB, VNC and UPnP. According to the documentation, a USB Message is sent from the MirrorLink Client (the car) to the MirrorLink Server (RasPi) before comunication is established.
VNC is used to mirror the Server's screen.

UPnP is used to exchange information needed to establish a VNC connection (and also for other things).

## What we don't know:
Is it possible without being a part of the CCC? 

How to handle the necessary certificates.

## TODO:
1. Handle MirrorLink USB Command
2. Make RasPi into a UPnP device and save all UPnP messages for further analysis.
3. Find information necesssary for establishing the VNC connection.
4. Find what information I'm missing.
