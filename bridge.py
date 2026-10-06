"""Windows tray bridge: SignalRGB UDP -> one Govee H6001 BLE bulb."""
from __future__ import annotations

import asyncio
import hmac
import secrets
import json
import logging
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pystray
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw
from PySide6.QtCore import QObject, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMainWindow,
    QPushButton,
    QSlider,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

SERVICE_UUID = "00010203-0405-0607-0809-0a0b0c0d1910"
WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 8765
TARGET_ADDRESS = "A4:C1:38:75:0B:F9"
TARGET_NAME = "Minger_H6001_0BF9"
STATUS_PORT = 8766
MIN_KELVIN = 2700
MAX_KELVIN = 6500
WEB_HOST = "0.0.0.0"
WEB_PORT = 8767


def configure_logging() -> logging.Logger:
    app_data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "GoveeSignalRGB"
    app_data.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(app_data / "bridge.log", maxBytes=1_000_000, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("govee-bridge")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


log = configure_logging()


def make_packet(command: int, payload: list[int]) -> bytes:
    frame = bytearray([0x33, command, *payload])
    if len(frame) > 19:
        raise ValueError("Govee-Befehl ist zu lang")
    frame.extend([0] * (19 - len(frame)))
    checksum = 0
    for value in frame:
        checksum ^= value
    frame.append(checksum)
    return bytes(frame)


def make_color_packet(rgb: list[int]) -> bytes:
    if len(rgb) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in rgb):
        raise ValueError("rgb muss drei Werte von 0 bis 255 enthalten")
    return make_packet(0x05, [0x02, *rgb])


def make_white_packet(kelvin: int) -> bytes:
    """Encode a Kelvin target in the H6001's dedicated WW channel mode."""
    kelvin = max(MIN_KELVIN, min(MAX_KELVIN, int(kelvin)))
    # These RGB triplets are H6001 white-mode command codes, not RGB output.
    # Interpolate them in reciprocal temperature (mired), where CCT mixing is
    # approximately linear, then set the protocol's dedicated WW flag below.
    warm = (0xFF, 0x93, 0x2C)  # 2700 K
    cool = (0xD7, 0xE2, 0xFF)  # 6500 K
    warm_mired = 1_000_000 / MIN_KELVIN
    cool_mired = 1_000_000 / MAX_KELVIN
    current_mired = 1_000_000 / kelvin
    fraction = (warm_mired - current_mired) / (warm_mired - cool_mired)
    white_rgb = [round(warm[i] + fraction * (cool[i] - warm[i])) for i in range(3)]
    # 0x01 selects the dedicated warm/cold white LEDs; the first RGB triple is
    # the ignored color-channel placeholder required by this H6001 packet.
    return make_packet(0x05, [0x02, 0xFF, 0xFF, 0xFF, 0x01, *white_rgb])


class Bridge(asyncio.DatagramProtocol):
    def __init__(self, app: "TrayApp") -> None:
        self.app = app
        self.transport: asyncio.DatagramTransport | None = None
        self.client: BleakClient | None = None
        self.lock: asyncio.Lock | None = None
        self.last_rgb: tuple[int, int, int] | None = None
        self.last_white: int | None = None
        self.last_send = 0.0
        self.power_on = True
        self.signalrgb_enabled = False
        self.mode = "white"
        self.kelvin = 2700

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.app.set_status("Bridge aktiv · verbinde Bluetooth …")

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            message = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.reply(addr, {"type": "error", "error": "Ungültiges JSON"})
            return
        asyncio.create_task(self.handle(message, addr))

    def reply(self, addr: tuple[str, int], message: dict[str, Any]) -> None:
        if self.transport:
            self.transport.sendto(json.dumps(message).encode("utf-8"), addr)

    def status(self) -> dict[str, Any]:
        return {
            "type": "status",
            "connected": bool(self.client and self.client.is_connected),
            "power": self.power_on,
            "signalrgb": self.signalrgb_enabled,
            "mode": self.mode,
            "kelvin": self.kelvin,
        }

    async def handle(self, message: dict[str, Any], addr: tuple[str, int]) -> None:
        command = message.get("command")
        try:
            if command == "status":
                self.reply(addr, self.status())
            elif command == "color":
                await self.set_color(message.get("rgb"))
            elif command == "power":
                await self.set_power(bool(message.get("on")))
                self.reply(addr, self.status())
            elif command == "set_signalrgb":
                await self.set_signalrgb(bool(message.get("enabled")))
                self.reply(addr, self.status())
            elif command == "white":
                await self.set_white(int(message.get("kelvin", 4000)))
                self.reply(addr, self.status())
            elif command == "disconnect":
                await self.disconnect()
                self.reply(addr, self.status())
            else:
                self.reply(addr, {"type": "error", "error": f"Unbekannter Befehl: {command}"})
        except Exception as exc:
            log.exception("Befehl %s fehlgeschlagen", command)
            self.reply(addr, {"type": "error", "error": str(exc)})

    async def connect(self) -> None:
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            if self.client and self.client.is_connected:
                return
            self.client = None
            log.info("Suche ausschließlich %s (%s)", TARGET_NAME, TARGET_ADDRESS)
            device = await BleakScanner.find_device_by_address(TARGET_ADDRESS, timeout=10.0)
            if device is None:
                self.app.set_status(f"{TARGET_NAME} nicht gefunden · erneuter Versuch folgt")
                return
            candidate = BleakClient(device)
            await candidate.connect(timeout=15.0)
            if not candidate.is_connected:
                raise RuntimeError("Bluetooth-Verbindung fehlgeschlagen")
            self.client = candidate
            self.last_rgb = None
            self.last_white = None
            log.info("Verbunden mit %s", TARGET_ADDRESS)
            self.app.set_status(f"Verbunden · {TARGET_NAME}")
            if self.power_on:
                await self._write(make_packet(0x01, [0x01]))
                if self.signalrgb_enabled:
                    self.app.set_mode("SignalRGB-Synchronisierung aktiv")
                else:
                    await self.set_white(self.kelvin, reset_color=True)

    async def reconnect_loop(self) -> None:
        while True:
            try:
                if not self.client or not self.client.is_connected:
                    await self.connect()
            except Exception as exc:
                log.warning("BLE-Verbindung fehlgeschlagen: %s", exc)
                self.app.set_status(f"Bluetooth-Fehler · {exc}")
            await asyncio.sleep(8)

    async def _write(self, packet: bytes) -> None:
        if not self.client or not self.client.is_connected:
            raise RuntimeError("H6001 ist nicht per Bluetooth verbunden")
        now = asyncio.get_running_loop().time()
        delay = 0.05 - (now - self.last_send)
        if delay > 0:
            await asyncio.sleep(delay)
        await self.client.write_gatt_char(WRITE_UUID, packet, response=False)
        self.last_send = asyncio.get_running_loop().time()

    async def set_color(self, rgb: Any) -> None:
        if not isinstance(rgb, list):
            raise ValueError("rgb muss [R,G,B] sein")
        packet = make_color_packet(rgb)
        if not self.signalrgb_enabled or not self.power_on:
            return
        color = tuple(rgb)
        if color == self.last_rgb or not self.client or not self.client.is_connected:
            return
        await self._write(packet)
        self.last_rgb = color
        # A new RGB frame replaced the previous white output. Force the saved
        # temperature to be written again when SignalRGB sync is paused.
        self.last_white = None
        self.mode = "rgb"
        self.app.set_mode("SignalRGB-Synchronisierung aktiv")

    async def set_power(self, enabled: bool) -> None:
        self.power_on = enabled
        if self.client and self.client.is_connected:
            await self._write(make_packet(0x01, [0x01 if enabled else 0x00]))
        self.app.set_power_state(enabled)
        if enabled:
            self.last_rgb = None
            self.last_white = None
        log.info("Lampe %s", "eingeschaltet" if enabled else "ausgeschaltet")

    async def set_signalrgb(self, enabled: bool) -> None:
        self.signalrgb_enabled = enabled
        self.mode = "rgb" if enabled else "white"
        self.last_rgb = None
        if enabled and not self.power_on:
            await self.set_power(True)
        if not enabled and self.power_on:
            # Apply the last selected white temperature immediately after the
            # checkbox is turned off, clearing any stale SignalRGB color first.
            await self.set_white(self.kelvin, reset_color=True)
        self.app.set_sync_state(enabled)
        log.info("SignalRGB-Synchronisierung %s", "aktiv" if enabled else "pausiert")

    async def set_white(self, kelvin: int, reset_color: bool = False) -> None:
        self.kelvin = max(MIN_KELVIN, min(MAX_KELVIN, int(kelvin)))
        self.signalrgb_enabled = False
        self.mode = "white"
        if not self.power_on:
            await self.set_power(True)
        if self.client and self.client.is_connected and (reset_color or self.last_white != self.kelvin):
            packet = make_white_packet(self.kelvin)
            await self._write(packet)
            self.last_white = self.kelvin
        self.app.set_sync_state(False)
        self.app.set_mode(f"Weißtemperatur · {self.kelvin} K")

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        if client and client.is_connected:
            await client.disconnect()
        self.app.set_status("Bluetooth getrennt")

    async def reconnect(self) -> None:
        self.app.set_status("Bluetooth wird neu verbunden …")
        await self.disconnect()
        await self.connect()

    async def prepare_to_exit(self) -> None:
        # Leave an illuminated lamp in cool WW mode before releasing BLE.
        if self.power_on and self.client and self.client.is_connected:
            try:
                await self.set_white(MAX_KELVIN, reset_color=True)
                await asyncio.sleep(0.3)
            except Exception:
                log.exception("Kaltweiß vor dem Beenden konnte nicht gesetzt werden")
        await self.disconnect()


MOBILE_PAGE = r'''<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#10131a"><title>Govee H6001</title>
<style>
:root{color-scheme:dark;font:16px system-ui,sans-serif;background:#10131a;color:#f4f6fb}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:18px}
main{width:min(100%,440px);background:#1a1f2a;border:1px solid #303849;border-radius:22px;padding:24px;box-shadow:0 16px 50px #0006}
h1{font-size:1.45rem;margin:0 0 4px}.muted{color:#aeb7c8}.status{padding:12px 14px;background:#111620;border-radius:12px;margin:18px 0}
label{display:block;margin:18px 0 8px;font-weight:650}.pinrow{display:flex;gap:8px}.pinrow input{flex:1}
input,button{font:inherit;border-radius:11px;border:1px solid #394355;background:#111620;color:#fff;padding:12px}
button{cursor:pointer;background:#476bff;border:0;font-weight:650}button:active{transform:scale(.98)}
.buttons{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}.buttons button.secondary{background:#303849}
input[type=range]{width:100%;padding:0;accent-color:#83a0ff}.temp{display:flex;justify-content:space-between;align-items:center}
.hidden{display:none}.msg{min-height:22px;color:#ffbd78;margin-top:12px;font-size:.9rem}
</style></head><body><main><h1>Govee H6001</h1><div class="muted">Bluetooth-Steuerung im lokalen WLAN</div>
<section id="login"><label for="pin">Zugangs-PIN vom Bridge-Fenster</label><div class="pinrow"><input id="pin" inputmode="numeric" maxlength="6" placeholder="6-stellige PIN"><button onclick="savePin()">Verbinden</button></div></section>
<section id="controls" class="hidden"><div class="status" id="status">Status wird geladen …</div>
<div class="buttons"><button id="power" onclick="togglePower()">Lampe …</button><button id="sync" class="secondary" onclick="toggleSync()">SignalRGB …</button></div>
<button class="secondary" style="width:100%;margin-top:10px" onclick="reconnect()">Bluetooth neu verbinden</button>
<label class="temp"><span>Weißtemperatur</span><strong id="kelvinLabel">2700 K</strong></label>
<input id="kelvin" type="range" min="2700" max="6500" step="50" value="2700" oninput="updateKelvinLabel()" onchange="setWhite()">
<div class="temp muted"><small>Warm · 2700 K</small><small>Kalt · 6500 K</small></div></section>
<div class="msg" id="message"></div></main>
<script>
let state={power:false,signalrgb:false,kelvin:2700};const pinInput=document.getElementById('pin');
function currentPin(){return sessionStorage.getItem('goveePin')||''}
function showControls(ok){document.getElementById('controls').classList.toggle('hidden',!ok);document.getElementById('login').classList.toggle('hidden',ok)}
async function api(path,body){const options={headers:{'X-Access-PIN':currentPin()}};if(body){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body)}
 const response=await fetch(path,options);const data=await response.json();if(!response.ok)throw Error(data.error||'Verbindung fehlgeschlagen');return data}
function savePin(){sessionStorage.setItem('goveePin',pinInput.value.trim());refresh()}
async function refresh(){try{const s=await api('/api/status');state=s;showControls(true);document.getElementById('status').textContent=s.connected?'Bluetooth verbunden':'Bluetooth wird verbunden …';document.getElementById('power').textContent=s.power?'Lampe ausschalten':'Lampe einschalten';document.getElementById('sync').textContent=s.signalrgb?'SignalRGB pausieren':'SignalRGB aktivieren';
 if(document.activeElement!==document.getElementById('kelvin'))document.getElementById('kelvin').value=s.kelvin||2700;updateKelvinLabel();document.getElementById('message').textContent='';}
 catch(e){showControls(false);if(currentPin())document.getElementById('message').textContent=e.message;}}
async function control(payload){try{state=await api('/api/control',payload);await refresh()}catch(e){document.getElementById('message').textContent=e.message}}
function togglePower(){control({command:'power',on:!state.power})}function toggleSync(){control({command:'set_signalrgb',enabled:!state.signalrgb})}
function reconnect(){control({command:'reconnect'})}
function updateKelvinLabel(){document.getElementById('kelvinLabel').textContent=document.getElementById('kelvin').value+' K'}
function setWhite(){control({command:'white',kelvin:Number(document.getElementById('kelvin').value)})}
const saved=sessionStorage.getItem('goveePin');if(saved){pinInput.value=saved;refresh()}setInterval(()=>{if(currentPin())refresh()},2500);
</script></body></html>'''


class TrayApp:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.bridge: Bridge | None = None
        self.icon: pystray.Icon | None = None
        self.status_text = "Starte Bridge …"
        self.mode_text = "Warmweiß · 2700 K"
        self._closing = False
        self.http_server: ThreadingHTTPServer | None = None
        self.access_pin = f"{secrets.randbelow(1_000_000):06d}"
        self.lan_url = f"http://{self._find_lan_ip()}:{WEB_PORT}"

    def start(self) -> None:
        threading.Thread(target=self._run_event_loop, name="GoveeBLE", daemon=True).start()
        self._start_web_server()
        self._start_tray()
        webbrowser.open("http://127.0.0.1:8767")
        if self.icon:
            self.icon.run()

    @staticmethod
    def _find_lan_ip() -> str:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 80))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            probe.close()

    def _start_web_server(self) -> None:
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def authorized(self) -> bool:
                supplied = self.headers.get("X-Access-PIN", "")
                if hmac.compare_digest(supplied, app.access_pin):
                    return True
                self.send_json(401, {"error": "PIN falsch oder fehlt"})
                return False

            def do_GET(self) -> None:
                if self.path == "/" or self.path.startswith("/?"):
                    body = MOBILE_PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/api/status":
                    if self.authorized():
                        self.send_json(200, app.bridge.status() if app.bridge else {"connected": False})
                    return
                self.send_json(404, {"error": "Nicht gefunden"})

            def do_POST(self) -> None:
                if self.path != "/api/control":
                    self.send_json(404, {"error": "Nicht gefunden"})
                    return
                if not self.authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 4096:
                        raise ValueError("Ungültige Anfragegröße")
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    command = payload.get("command")
                    bridge = app.bridge
                    if bridge is None or not app.loop.is_running():
                        raise RuntimeError("Bridge ist noch nicht gestartet")
                    if command == "power" and type(payload.get("on")) is bool:
                        operation = bridge.set_power(payload["on"])
                    elif command == "set_signalrgb" and type(payload.get("enabled")) is bool:
                        operation = bridge.set_signalrgb(payload["enabled"])
                    elif command == "white":
                        operation = bridge.set_white(int(payload.get("kelvin", MIN_KELVIN)))
                    elif command == "reconnect":
                        operation = bridge.reconnect()
                    else:
                        raise ValueError("Unbekannter oder ungültiger Befehl")
                    future = asyncio.run_coroutine_threadsafe(operation, app.loop)
                    future.result(timeout=8)
                    self.send_json(200, bridge.status())
                except Exception as exc:
                    log.exception("Handy-Websteuerung fehlgeschlagen")
                    self.send_json(400, {"error": str(exc)})

        try:
            self.http_server = ThreadingHTTPServer((WEB_HOST, WEB_PORT), Handler)
            self.http_server.daemon_threads = True
            threading.Thread(target=self.http_server.serve_forever,
                             name="GoveeWebServer", daemon=True).start()
            log.info("Mobile UI lauscht unter %s:%d", WEB_HOST, WEB_PORT)
        except OSError as exc:
            log.exception("Mobile Webserver konnte nicht gestartet werden")
            self.set_status(f"Webserver-Port {WEB_PORT} belegt: {exc}")

    def _run_event_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.bridge = Bridge(self)
        self.loop.create_task(self._start_bridge())
        self.loop.run_forever()

    async def _start_bridge(self) -> None:
        try:
            transport, _ = await self.loop.create_datagram_endpoint(
                lambda: self.bridge, local_addr=(BRIDGE_HOST, BRIDGE_PORT)
            )
            self.transport = transport
            log.info("UDP bridge bound to %s:%d", BRIDGE_HOST, BRIDGE_PORT)
            self.loop.create_task(self.bridge.reconnect_loop())
        except OSError as exc:
            log.exception("Bridge konnte nicht gestartet werden")
            self.set_status(f"UDP-Port {BRIDGE_PORT} belegt: {exc}")

    def _start_tray(self) -> None:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=(80, 170, 255, 255), outline=(245, 245, 245, 255), width=3)
        draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Steuerung im Browser öffnen", self.show_window, default=True),
            pystray.MenuItem(f"Handy: {self.lan_url}", None, enabled=False),
            pystray.MenuItem(f"PIN: {self.access_pin}", None, enabled=False),
            pystray.MenuItem("Lampe Ein/Aus", self.tray_toggle_power),
            pystray.MenuItem("SignalRGB-Sync umschalten", self.tray_toggle_sync),
            pystray.MenuItem("Bluetooth neu verbinden", self.tray_reconnect),
            pystray.MenuItem("Beenden · WW-Kaltweiß", self.quit),
        )
        self.icon = pystray.Icon("Govee H6001 Bridge", image, "Govee H6001 Bridge", menu)
        

    def submit(self, coroutine: Any) -> None:
        if self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

            def report_error(result: Any) -> None:
                try:
                    result.result()
                except Exception as exc:
                    log.exception("UI-Befehl fehlgeschlagen")
                    self.set_status(f"Befehl fehlgeschlagen · {exc}")

            future.add_done_callback(report_error)

    def toggle_power(self) -> None:
        if self.bridge:
            self.submit(self.bridge.set_power(not self.bridge.power_on))

    def tray_toggle_power(self, _icon: Any, _item: Any) -> None:
        self.toggle_power()

    def tray_toggle_sync(self, _icon: Any, _item: Any) -> None:
        if self.bridge:
            self.submit(self.bridge.set_signalrgb(not self.bridge.signalrgb_enabled))

    def tray_reconnect(self, _icon: Any, _item: Any) -> None:
        self.reconnect()

    def toggle_sync(self) -> None:
        if self.bridge:
            self.submit(self.bridge.set_signalrgb(not self.bridge.signalrgb_enabled))

    def reconnect(self) -> None:
        if self.bridge:
            self.submit(self.bridge.reconnect())

    def set_status(self, message: str) -> None:
        log.info("Status: %s", message)
        self.status_text = message

    def set_mode(self, message: str) -> None:
        self.mode_text = message

    def set_power_state(self, enabled: bool) -> None:
        if self.icon:
            self.icon.update_menu()

    def set_sync_state(self, enabled: bool) -> None:
        if self.icon:
            self.icon.update_menu()

    def show_window(self, _icon: Any = None, _item: Any = None) -> None:
        webbrowser.open("http://127.0.0.1:8767")

    def quit(self, _icon: Any = None, _item: Any = None) -> None:
        if self._closing:
            return
        self._closing = True
        if self.bridge and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.bridge.prepare_to_exit(), self.loop)
            try:
                future.result(timeout=5)
            except Exception:
                pass
        if self.icon:
            self.icon.stop()
        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)


class GuiSignals(QObject):
    status = Signal(str)
    mode = Signal(str)
    power = Signal(bool)
    sync = Signal(bool)


class NativeTrayApp(TrayApp):
    """Native Windows desktop window; the web page remains for phones."""

    def __init__(self) -> None:
        super().__init__()
        self.qt_app: QApplication | None = None
        self.window: QMainWindow | None = None
        self.tray_icon: QSystemTrayIcon | None = None
        self.signals = GuiSignals()
        self.status_label: QLabel | None = None
        self.mode_label: QLabel | None = None
        self.power_button: QPushButton | None = None
        self.sync_checkbox: QCheckBox | None = None
        self.kelvin_slider: QSlider | None = None
        self.kelvin_label: QLabel | None = None
        self._white_timer: QTimer | None = None

    def start(self) -> None:
        self.qt_app = QApplication(sys.argv)
        self.qt_app.setQuitOnLastWindowClosed(False)
        self.signals.status.connect(self._show_status)
        self.signals.mode.connect(self._show_mode)
        self.signals.power.connect(self._show_power)
        self.signals.sync.connect(self._show_sync)
        self._build_native_window()
        threading.Thread(target=self._run_event_loop, name="GoveeBLE", daemon=True).start()
        self._start_web_server()
        self._start_native_tray()
        assert self.window is not None
        self.window.show()
        self.qt_app.exec()

    def _build_native_window(self) -> None:
        class MainWindow(QMainWindow):
            def closeEvent(self, event: Any) -> None:
                event.ignore()
                self.hide()

        self.window = MainWindow()
        self.window.setWindowTitle("Govee H6001 Bluetooth")
        self.window.setFixedSize(430, 460)
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)

        title = QLabel("Govee H6001")
        title.setObjectName("title")
        layout.addWidget(title)
        self.status_label = QLabel("Starte Bridge …")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        row = QHBoxLayout()
        self.power_button = QPushButton("Lampe ausschalten")
        self.power_button.clicked.connect(lambda _checked=False: self.toggle_power())
        row.addWidget(self.power_button)
        self.sync_checkbox = QCheckBox("SignalRGB-Sync")
        self.sync_checkbox.toggled.connect(self._sync_changed)
        row.addWidget(self.sync_checkbox)
        layout.addLayout(row)

        reconnect_button = QPushButton("Bluetooth neu verbinden")
        reconnect_button.clicked.connect(lambda _checked=False: self.reconnect())
        layout.addWidget(reconnect_button)

        temp_row = QHBoxLayout()
        temp_title = QLabel("Weißtemperatur")
        temp_title.setObjectName("section")
        self.kelvin_label = QLabel("2700 K")
        temp_row.addWidget(temp_title)
        temp_row.addStretch(1)
        temp_row.addWidget(self.kelvin_label)
        layout.addLayout(temp_row)
        self.kelvin_slider = QSlider(Qt.Orientation.Horizontal)
        self.kelvin_slider.setRange(MIN_KELVIN, MAX_KELVIN)
        self.kelvin_slider.setSingleStep(50)
        self.kelvin_slider.setPageStep(200)
        self.kelvin_slider.setValue(2700)
        self.kelvin_slider.valueChanged.connect(self._temperature_changed)
        layout.addWidget(self.kelvin_slider)
        ends = QHBoxLayout()
        ends.addWidget(QLabel("Warm · 2700 K"))
        ends.addStretch(1)
        ends.addWidget(QLabel("Kalt · 6500 K"))
        layout.addLayout(ends)
        self.mode_label = QLabel("Warmweiß · 2700 K")
        self.mode_label.setObjectName("muted")
        layout.addWidget(self.mode_label)

        layout.addSpacing(4)
        phone_title = QLabel("Handy-Steuerung im selben WLAN")
        phone_title.setObjectName("section")
        layout.addWidget(phone_title)
        layout.addWidget(QLabel(self.lan_url))
        pin_row = QHBoxLayout()
        pin_row.addWidget(QLabel(f"PIN: <b>{self.access_pin}</b>"))
        copy_button = QPushButton("PIN kopieren")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.access_pin))
        pin_row.addWidget(copy_button)
        pin_row.addStretch(1)
        layout.addLayout(pin_row)
        layout.addWidget(QLabel("Schließen blendet das Fenster nur in den Infobereich aus."))

        exit_button = QPushButton("Beenden · Lampe auf WW-Kaltweiß")
        exit_button.setObjectName("exit")
        exit_button.clicked.connect(lambda _checked=False: self.quit())
        layout.addWidget(exit_button)
        layout.addStretch(1)
        self.window.setCentralWidget(central)
        self.window.setStyleSheet("""
            QWidget { background: #f4f6fa; color: #202633; font: 10pt 'Segoe UI'; }
            QLabel#title { font-size: 20pt; font-weight: 700; }
            QLabel#section { font-size: 11pt; font-weight: 650; }
            QLabel#muted { color: #647084; }
            QPushButton { background: #e5eaf2; border: 1px solid #cbd3df; border-radius: 8px; padding: 9px 12px; }
            QPushButton:hover { background: #dbe4f2; }
            QPushButton#exit { background: #385fca; color: white; font-weight: 650; }
            QSlider::groove:horizontal { height: 6px; background: #cbd3df; border-radius: 3px; }
            QSlider::handle:horizontal { width: 18px; margin: -7px 0; border-radius: 9px; background: #385fca; }
            QCheckBox { spacing: 7px; }
        """)
        self._white_timer = QTimer(self.window)
        self._white_timer.setSingleShot(True)
        self._white_timer.setInterval(150)
        self._white_timer.timeout.connect(self._apply_slider_temperature)

    def _start_native_tray(self) -> None:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(80, 150, 245))
        painter.setPen(QColor(245, 245, 245))
        painter.drawEllipse(7, 7, 50, 50)
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(22, 22, 20, 20)
        painter.end()

        menu = QMenu()
        open_action = QAction("Govee H6001 öffnen", menu)
        open_action.triggered.connect(self.show_window)
        menu.addAction(open_action)
        menu.addSeparator()
        power_action = QAction("Lampe Ein/Aus", menu)
        power_action.triggered.connect(lambda _checked=False: self.toggle_power())
        menu.addAction(power_action)
        sync_action = QAction("SignalRGB-Sync umschalten", menu)
        sync_action.triggered.connect(lambda _checked=False: self._sync_changed(
            not (self.bridge.signalrgb_enabled if self.bridge else False)))
        menu.addAction(sync_action)
        reconnect_action = QAction("Bluetooth neu verbinden", menu)
        reconnect_action.triggered.connect(lambda _checked=False: self.reconnect())
        menu.addAction(reconnect_action)
        menu.addSeparator()
        exit_action = QAction("Beenden · WW-Kaltweiß", menu)
        exit_action.triggered.connect(self.quit)
        menu.addAction(exit_action)

        self.tray_icon = QSystemTrayIcon(QIcon(pixmap), self.window)
        self.tray_icon.setToolTip("Govee H6001 Bluetooth")
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self.tray_icon.show()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.show_window()

    def _temperature_changed(self, kelvin: int) -> None:
        if self.kelvin_label:
            self.kelvin_label.setText(f"{kelvin} K")
        if self._white_timer:
            self._white_timer.start()

    def _apply_slider_temperature(self) -> None:
        if not self.bridge or not self.kelvin_slider:
            return
        kelvin = self.kelvin_slider.value()
        if self.sync_checkbox:
            blocker = QSignalBlocker(self.sync_checkbox)
            self.sync_checkbox.setChecked(False)
            del blocker
        self.submit(self.bridge.set_white(kelvin))

    def _sync_changed(self, enabled: bool) -> None:
        if self.bridge:
            self.submit(self.bridge.set_signalrgb(enabled))

    def submit(self, coroutine: Any) -> None:
        if self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

            def report_error(result: Any) -> None:
                try:
                    result.result()
                except Exception as exc:
                    log.exception("Desktop-Befehl fehlgeschlagen")
                    self.set_status(f"Befehl fehlgeschlagen · {exc}")

            future.add_done_callback(report_error)

    def toggle_power(self, *_args: Any) -> None:
        if self.bridge:
            self.submit(self.bridge.set_power(not self.bridge.power_on))

    def reconnect(self, *_args: Any) -> None:
        if self.bridge:
            self.submit(self.bridge.reconnect())

    def set_status(self, message: str) -> None:
        log.info("Status: %s", message)
        self.status_text = message
        self.signals.status.emit(message)

    def set_mode(self, message: str) -> None:
        self.mode_text = message
        self.signals.mode.emit(message)

    def set_power_state(self, enabled: bool) -> None:
        self.signals.power.emit(enabled)

    def set_sync_state(self, enabled: bool) -> None:
        self.signals.sync.emit(enabled)

    def _show_status(self, message: str) -> None:
        if self.status_label:
            self.status_label.setText(message)

    def _show_mode(self, message: str) -> None:
        if self.mode_label:
            self.mode_label.setText(message)

    def _show_power(self, enabled: bool) -> None:
        if self.power_button:
            self.power_button.setText("Lampe ausschalten" if enabled else "Lampe einschalten")

    def _show_sync(self, enabled: bool) -> None:
        if self.sync_checkbox:
            blocker = QSignalBlocker(self.sync_checkbox)
            self.sync_checkbox.setChecked(enabled)
            del blocker

    def show_window(self, *_args: Any) -> None:
        if self.window:
            self.window.show()
            self.window.raise_()
            self.window.activateWindow()

    def quit(self, *_args: Any) -> None:
        if self._closing:
            return
        self._closing = True
        if self.bridge and self.loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self.bridge.prepare_to_exit(), self.loop)
            try:
                future.result(timeout=5)
            except Exception:
                log.exception("Fehler beim Beenden der Bridge")
        if self.tray_icon:
            self.tray_icon.hide()
        if self.http_server:
            self.http_server.shutdown()
            self.http_server.server_close()
        if self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.qt_app:
            self.qt_app.quit()


if __name__ == "__main__":
    try:
        NativeTrayApp().start()
    except Exception:
        log.exception("Tray-App konnte nicht gestartet werden")
