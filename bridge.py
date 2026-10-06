"""Local BLE bridge for Govee H6001; SignalRGB talks to it over localhost UDP."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from bleak import BleakClient, BleakScanner

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
TARGET_ADDRESS = "A4:C1:38:75:0B:F9"
TARGET_NAME = "Minger_H6001_0BF9"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("govee-ble-bridge")


def make_color_packet(rgb: list[int]) -> bytes:
    """Encode the H6001 manual RGB command (0x33 frame, XOR checksum)."""
    if len(rgb) != 3 or any(type(channel) is not int or not 0 <= channel <= 255 for channel in rgb):
        raise ValueError("rgb must be three integers from 0 to 255")
    packet = bytearray([0x33, 0x05, 0x02, *rgb, 0x00])
    packet.extend([0] * (19 - len(packet)))
    checksum = 0
    for byte in packet:
        checksum ^= byte
    packet.append(checksum)
    return bytes(packet)


class Bridge(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.transport: asyncio.DatagramTransport | None = None
        self.client: BleakClient | None = None
        self.lock = asyncio.Lock()
        self.last_rgb: tuple[int, int, int] | None = None
        self.last_send = 0.0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        log.info("Bridge lauscht auf %s:%s", BRIDGE_HOST, BRIDGE_PORT)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.reply(addr, {"type": "error", "error": "Ungültiges JSON"})
            return
        log.info("UDP-Befehl von %s:%s: %s", addr[0], addr[1], message.get("command", "?"))
        asyncio.create_task(self.handle(message, addr))

    def reply(self, addr: tuple[str, int], message: dict[str, Any]) -> None:
        if self.transport:
            if message.get("type") == "status":
                log.info("Status-Antwort an %s:%s: connected=%s",
                         addr[0], addr[1], message.get("connected"))
            self.transport.sendto(json.dumps(message).encode("utf-8"), addr)

    async def handle(self, message: dict[str, Any], addr: tuple[str, int]) -> None:
        command = message.get("command")
        try:
            if command == "scan":
                found = await BleakScanner.discover(timeout=6.0)
                devices = [{"name": d.name or TARGET_NAME, "address": d.address,
                            "rssi": getattr(d, "rssi", None)} for d in found
                           if d.address.casefold() == TARGET_ADDRESS.casefold()]
                self.reply(addr, {"type": "scan_result", "devices": devices})
            elif command == "connect":
                await self.connect(str(message.get("address", "")))
                self.reply(addr, {"type": "status", "connected": True,
                                 "name": self.client.address if self.client else ""})
            elif command == "disconnect":
                await self.disconnect()
                self.reply(addr, {"type": "status", "connected": False})
            elif command == "color":
                await self.set_color(message.get("rgb"))
            elif command == "status":
                self.reply(addr, {"type": "status", "connected": bool(self.client and self.client.is_connected)})
            else:
                self.reply(addr, {"type": "error", "error": f"Unbekannter Befehl: {command}"})
        except Exception as exc:
            log.exception("Befehl fehlgeschlagen")
            self.reply(addr, {"type": "error", "error": str(exc)})

    async def connect(self, address: str) -> None:
        if not address:
            raise ValueError("Keine Bluetooth-Adresse angegeben")
        if address.casefold() != TARGET_ADDRESS.casefold():
            raise ValueError(f"Diese Bridge erlaubt nur {TARGET_NAME} ({TARGET_ADDRESS})")
        async with self.lock:
            await self.disconnect()
            device = await BleakScanner.find_device_by_address(address, timeout=8.0)
            if device is None:
                raise RuntimeError("Gerät nicht gefunden; Lampe einschalten und erneut suchen")
            self.client = BleakClient(device)
            await self.client.connect(timeout=15.0)
            if not self.client.is_connected:
                self.client = None
                raise RuntimeError("Bluetooth-Verbindung fehlgeschlagen")
            self.last_rgb = None
            log.info("Verbunden mit %s", address)

    async def disconnect(self) -> None:
        if self.client:
            try:
                if self.client.is_connected:
                    await self.client.disconnect()
            finally:
                self.client = None
        self.last_rgb = None

    async def set_color(self, rgb: Any) -> None:
        if not isinstance(rgb, list):
            raise ValueError("rgb muss [R,G,B] sein")
        packet = make_color_packet(rgb)
        if not self.client or not self.client.is_connected:
            raise RuntimeError("H6001 ist nicht per Bluetooth verbunden")
        color = tuple(rgb)
        now = asyncio.get_running_loop().time()
        # Ignore unchanged frames; cap writes at 20 per second for BLE stability.
        if color == self.last_rgb:
            return
        delay = 0.05 - (now - self.last_send)
        if delay > 0:
            await asyncio.sleep(delay)
        await self.client.write_gatt_char(WRITE_UUID, packet, response=False)
        self.last_send = asyncio.get_running_loop().time()
        self.last_rgb = color
        log.info("Farbe an H6001 gesendet: #%02X%02X%02X", *rgb)


async def main() -> None:
    loop = asyncio.get_running_loop()
    bridge = Bridge()
    print(f"Suche ausschließlich {TARGET_NAME} ({TARGET_ADDRESS}) …")
    device = await BleakScanner.find_device_by_address(TARGET_ADDRESS, timeout=12.0)
    if device is None:
        raise RuntimeError(
            f"{TARGET_NAME} ({TARGET_ADDRESS}) nicht gefunden. "
            "Lampe einschalten, in Bluetooth-Reichweite bringen und erneut starten."
        )
    await bridge.connect(device.address)
    transport, _ = await loop.create_datagram_endpoint(
        lambda: bridge, local_addr=(BRIDGE_HOST, BRIDGE_PORT)
    )
    print("Verbunden. SignalRGB kann jetzt Farben senden. Beenden mit Strg+C.")
    try:
        await asyncio.Future()
    finally:
        await bridge.disconnect()
        transport.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bridge beendet")
