# Govee Bluetooth for SignalRGB (prototype)

This repository contains a SignalRGB Add-on source file and a local Bluetooth
bridge. The current setup is locked to the user's Minger H6001 at
`A4:C1:38:75:0B:F9`; other BLE devices are ignored. SignalRGB loads the add-on
source; the bridge controls the bulb over BLE because SignalRGB's documented
add-on communication API does not expose BLE directly.

## Files

- `GoveeBluetoothAddon.js` — SignalRGB Add-on source. Polls the bridge status,
  announces the virtual device only after BLE is connected, provides lighting
  controls/settings, and sends canvas color changes over UDP.
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

The bridge searches only for `Minger_H6001_0BF9` at `A4:C1:38:75:0B:F9`, connects
automatically, and listens only on `127.0.0.1:8765`. It accepts JSON UDP
messages:

- `{"command":"scan"}` — scan nearby BLE devices and reply to the sender.
- `{"command":"connect","address":"AA:BB:CC:DD:EE:FF"}` — connect to a device.
- `{"command":"color","rgb":[255,0,0]}` — set the bulb color.
- `{"command":"disconnect"}` — disconnect.

Start the bridge before activating the SignalRGB add-on. After the BLE link is
up, the add-on should announce a separate H6001 device in SignalRGB; its lighting
page includes the output toggle and frame delay setting. The add-on panel shows
setup information. To target a different bulb later, change `TARGET_ADDRESS`
and `TARGET_NAME` in `bridge.py` and `TARGET_ADDRESS` in the add-on source.

## SignalRGB Add-on

Add this GitHub repository as a SignalRGB Add-on after pushing it to GitHub.
SignalRGB's Add-on loader and UI have version-specific expectations, so the
source may need minor adjustments for the installed SignalRGB version. Version
0.2.0 now includes the discovery service needed to announce the virtual H6001
device. It has not yet been tested in SignalRGB or against the physical bulb.

## H6001 protocol

The encoder uses the H6001 manual color command documented by community
reverse-engineering work: `0x33` prefix, command `0x05`, manual mode `0x02`, RGB
bytes, zero padding, and XOR checksum. H6001 hardware revisions and Bluetooth
adapter behavior still need verification.
