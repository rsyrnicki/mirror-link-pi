# Prepare Raspberry Pi Zero 2W

## Fix Wifi problem on some RPi02W:
sudo nano /etc/modprobe.d/brcmfmac.conf
```
options brcmfmac feature_disable=0x2000
```

## Install Raspberry Pi OS

## Increase SWAP

## Configure usb gadget mode using libcomposite

## Configure DHCP Server on usb0 interface
https://www.elektronik-kompendium.de/sites/raspberry-pi/2202031.htm

## Activate VNC

## Allow UPnP Communication
