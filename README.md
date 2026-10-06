# Govee Bluetooth for SignalRGB (prototype)

This repository contains a SignalRGB Add-on source file and a local Bluetooth
bridge. SignalRGB loads the add-on source; the bridge connects to a Govee H6001
over BLE because SignalRGB's documented add-on communication API does not expose
BLE directly.

## Files

- `GoveeBluetoothAddon.js` — SignalRGB device source. Samples the one-pixel
  canvas and sends color changes to the local bridge over UDP.
- `GoveeBluetoothAddon.qml` — Add-on information panel.
- `bridge.py` — local BLE bridge, device scanner, and H6001 command encoder.

## Start the bridge

Requires Windows 10/11, Python 3.10+, Bluetooth LE, and the H6001 powered on.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
py bridge.py
```

The bridge scans at startup, shows nearby Bluetooth devices, and asks you to
select the H6001. After connecting, it listens only on `127.0.0.1:8765` and
accepts JSON UDP messages:

- `{"command":"scan"}` — scan nearby BLE devices and reply to the sender.
- `{"command":"connect","address":"AA:BB:CC:DD:EE:FF"}` — connect to a device.
- `{"command":"color","rgb":[255,0,0]}` — set the bulb color.
- `{"command":"disconnect"}` — disconnect.

Start the bridge before activating the SignalRGB add-on. The add-on sends color
updates automatically after the BLE connection is established. The Add-on
panel currently displays setup information; device selection happens in the
bridge console.

## SignalRGB Add-on

Add this GitHub repository as a SignalRGB Add-on after pushing it to GitHub.
SignalRGB's Add-on loader and UI have version-specific expectations, so the
source may need minor adjustments for the installed SignalRGB version. The
current source files provide the Add-on bundle foundation; they have not yet
been tested in SignalRGB or against a physical H6001.

## H6001 protocol

The encoder uses the H6001 manual color command documented by community
reverse-engineering work: `0x33` prefix, command `0x05`, manual mode `0x02`, RGB
bytes, zero padding, and XOR checksum. H6001 hardware revisions and Bluetooth
adapter behavior still need verification.
