"""Windows tray bridge: SignalRGB UDP -> one Govee H6001 BLE bulb."""
from __future__ import annotations

import asyncio
import hmac
import secrets
import json
import logging
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tkinter as tk
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import ttk
from typing import Any

import pystray
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw

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
        self.root: tk.Tk | None = None
        self.icon: pystray.Icon | None = None
        self.status_var: tk.StringVar | None = None
        self.mode_var: tk.StringVar | None = None
        self.sync_var: tk.BooleanVar | None = None
        self.power_button: ttk.Button | None = None
        self.temperature_var: tk.IntVar | None = None
        self.temperature_value: ttk.Label | None = None
        self.pin_copy_var: tk.StringVar | None = None
        self._white_after: str | None = None
        self._closing = False
        self.http_server: ThreadingHTTPServer | None = None
        self.access_pin = f"{secrets.randbelow(1_000_000):06d}"
        self.lan_url = f"http://{self._find_lan_ip()}:{WEB_PORT}"

    def start(self) -> None:
        threading.Thread(target=self._run_event_loop, name="GoveeBLE", daemon=True).start()
        self.root = tk.Tk()
        self.root.title("Govee H6001 Bridge")
        self.root.geometry("410x500")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._build_ui()
        self._start_tray()
        self._start_web_server()
        self.root.mainloop()

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

    def _build_ui(self) -> None:
        assert self.root is not None
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Govee H6001", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Handy-Steuerung im selben WLAN", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 0))
        ttk.Label(outer, text=f"Adresse: {self.lan_url}").pack(anchor="w", pady=(2, 0))
        pin_row = ttk.Frame(outer)
        pin_row.pack(fill="x", pady=(2, 0))
        ttk.Label(pin_row, text=f"PIN: {self.access_pin}", font=("Segoe UI", 11, "bold")).pack(side="left")
        ttk.Button(pin_row, text="PIN kopieren", command=self.copy_pin).pack(side="left", padx=(10, 0))
        self.pin_copy_var = tk.StringVar(value="")
        ttk.Label(pin_row, textvariable=self.pin_copy_var, foreground="#287a36").pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="Starte Bridge …")
        ttk.Label(outer, textvariable=self.status_var, wraplength=370).pack(anchor="w", pady=(6, 14))

        controls = ttk.Frame(outer)
        controls.pack(fill="x")
        self.power_button = ttk.Button(controls, text="Lampe ausschalten", command=self.toggle_power)
        self.power_button.pack(side="left")
        self.sync_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(controls, text="SignalRGB-Sync", variable=self.sync_var,
                        command=self.toggle_sync).pack(side="right")
        ttk.Button(outer, text="Bluetooth neu verbinden", command=self.reconnect).pack(anchor="w", pady=(8, 0))

        ttk.Separator(outer).pack(fill="x", pady=16)
        ttk.Label(outer, text="Weißtemperatur", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.temperature_var = tk.IntVar(value=2700)
        self.temperature_value = ttk.Label(outer, text="2700 K")
        self.temperature_value.pack(anchor="e")
        slider = ttk.Scale(outer, from_=MIN_KELVIN, to=MAX_KELVIN, orient="horizontal",
                           command=self.on_temperature_change)
        slider.set(2700)
        slider.pack(fill="x", pady=(0, 6))
        ttk.Label(outer, text="Warm 2700 K                              Kalt 6500 K").pack(anchor="w")

        self.mode_var = tk.StringVar(value="Warmweiß · 2700 K")
        ttk.Label(outer, textvariable=self.mode_var, foreground="#555555").pack(anchor="w", pady=(14, 0))
        ttk.Label(outer, text="Schließen blendet das Fenster nur in den Infobereich aus.",
                  foreground="#777777", wraplength=370).pack(anchor="w", pady=(8, 0))
        ttk.Button(outer, text="Beenden · Lampe auf WW-Kaltweiß", command=self.quit).pack(anchor="w", pady=(12, 0))

    def copy_pin(self) -> None:
        if not self.root:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.access_pin)
        self.root.update_idletasks()
        if self.pin_copy_var:
            self.pin_copy_var.set("Kopiert")

    def _start_tray(self) -> None:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=(80, 170, 255, 255), outline=(245, 245, 245, 255), width=3)
        draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Govee H6001 öffnen", self.show_window, default=True),
            pystray.MenuItem("Lampe Ein/Aus", self.tray_toggle_power),
            pystray.MenuItem("Beenden · WW-Kaltweiß", self.quit),
        )
        self.icon = pystray.Icon("Govee H6001 Bridge", image, "Govee H6001 Bridge", menu)
        self.icon.run_detached()

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

    def toggle_sync(self) -> None:
        if self.bridge and self.sync_var:
            self.submit(self.bridge.set_signalrgb(self.sync_var.get()))

    def reconnect(self) -> None:
        if self.bridge:
            self.submit(self.bridge.reconnect())

    def on_temperature_change(self, value: str) -> None:
        kelvin = round(float(value) / 50) * 50
        if self.temperature_value:
            self.temperature_value.configure(text=f"{kelvin} K")
        if self._white_after and self.root:
            self.root.after_cancel(self._white_after)
        if self.root:
            self._white_after = self.root.after(180, lambda: self.apply_white(kelvin))

    def apply_white(self, kelvin: int) -> None:
        self._white_after = None
        if self.sync_var:
            self.sync_var.set(False)
        log.info("Weißtemperatur-Regler: %d K", kelvin)
        if self.bridge:
            self.submit(self.bridge.set_white(kelvin))

    def set_status(self, message: str) -> None:
        log.info("Status: %s", message)
        if self.root and self.status_var:
            self.root.after(0, lambda: self.status_var.set(message) if self.status_var else None)

    def set_mode(self, message: str) -> None:
        if self.root and self.mode_var:
            self.root.after(0, lambda: self.mode_var.set(message) if self.mode_var else None)

    def set_power_state(self, enabled: bool) -> None:
        if self.root and self.power_button:
            label = "Lampe ausschalten" if enabled else "Lampe einschalten"
            self.root.after(0, lambda: self.power_button.configure(text=label) if self.power_button else None)

    def set_sync_state(self, enabled: bool) -> None:
        if self.root and self.sync_var:
            self.root.after(0, lambda: self.sync_var.set(enabled) if self.sync_var else None)

    def show_window(self, _icon: Any = None, _item: Any = None) -> None:
        if self.root:
            self.root.after(0, self.root.deiconify)
            self.root.after(0, self.root.lift)

    def hide_window(self) -> None:
        if self.root:
            self.root.withdraw()

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
        if self.root:
            self.root.after(0, self.root.destroy)


if __name__ == "__main__":
    try:
        TrayApp().start()
    except Exception:
        log.exception("Tray-App konnte nicht gestartet werden")
