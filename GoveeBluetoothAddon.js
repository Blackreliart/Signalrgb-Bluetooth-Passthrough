// SignalRGB network device component distributed as a SignalRGB Add-on.
// The companion bridge performs BLE; this file only samples the canvas and
// sends local UDP messages to 127.0.0.1:8765.
import udp from "@SignalRGB/udp";

const BRIDGE_HOST = "127.0.0.1";
const BRIDGE_PORT = 8765;
let socket;
let connected = false;
let lastColor = "";

export function Name() { return "Govee H6001 Bluetooth"; }
export function Version() { return "0.1.0"; }
export function Publisher() { return "Community"; }
export function Type() { return "network"; }
export function Size() { return [1, 1]; }
export function LedNames() { return ["H6001 Bulb"]; }
export function LedPositions() { return [[0, 0]]; }
export function DefaultPosition() { return [0, 70]; }
export function DefaultScale() { return 1.0; }
export function Validate() { return true; }

export function ControllableParameters() {
    return [
        { property: "enableOutput", group: "lighting", label: "Send colors to H6001", type: "boolean", default: true },
        { property: "frameDelay", group: "settings", label: "Minimum delay (ms)", type: "number", min: "50", max: "500", step: "25", default: "50" }
    ];
}

export function Initialize() {
    socket = udp.createSocket();
    connected = true;
    device.log("Govee Bluetooth Add-on started. Check the local BLE bridge.");
}

export function Render() {
    if (!connected || !enableOutput || !socket) return;
    const sample = device.color(0, 0);
    if (!sample || sample.length < 3) return;
    const rgb = sample.slice(0, 3).map((channel) =>
        Math.max(0, Math.min(255, Math.round(channel)))
    );
    const key = rgb.join(",");
    if (key === lastColor) return;
    socket.write(JSON.stringify({ command: "color", rgb }), BRIDGE_HOST, BRIDGE_PORT);
    lastColor = key;
}

export function Shutdown() {
    connected = false;
    if (socket) socket.close();
}
