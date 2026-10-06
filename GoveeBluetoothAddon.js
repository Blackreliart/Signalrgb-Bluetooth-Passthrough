// SignalRGB Add-on for one fixed Govee H6001 BLE bulb.
// DiscoveryService announces a virtual network controller to SignalRGB;
// Initialize/Render sample its one LED and send colors to the local BLE bridge.
import udp from "@SignalRGB/udp";

const TARGET_ADDRESS = "A4:C1:38:75:0B:F9";
const BRIDGE_HOST = "127.0.0.1";
const BRIDGE_PORT = 8765;
const CONTROLLER_ID = `govee-h6001-${TARGET_ADDRESS.replaceAll(":", "").toLowerCase()}`;

export function Name() { return "Govee H6001 Bluetooth"; }
export function Version() { return "0.2.0"; }
export function Publisher() { return "Community"; }
export function Type() { return "network"; }
export function Size() { return [1, 1]; }
export function LedNames() { return ["H6001 Bulb"]; }
export function LedPositions() { return [[0, 0]]; }
export function DefaultPosition() { return [0, 70]; }
export function DefaultScale() { return 1.0; }
export function DefaultComponentBrand() { return "Govee"; }
export function Validate() { return true; }
export function SubdeviceController() { return false; }

export function ControllableParameters() {
    return [
        { property: "enableOutput", group: "lighting", label: "Send colors to H6001", type: "combobox", values: ["Enabled", "Disabled"], default: "Enabled" },
        { property: "frameDelay", group: "settings", label: "Minimum frame delay (ms)", type: "combobox", values: ["50", "100", "200"], default: "50" }
    ];
}

class H6001Controller {
    constructor() {
        this.device = {
            id: CONTROLLER_ID,
            address: TARGET_ADDRESS,
            name: "Minger H6001 (Bluetooth)",
            leds: 1,
            getName() { return this.name; }
        };
        this.id = CONTROLLER_ID;
        this.name = this.device.name;
        this.changed = false;
        this.connected = false;
        this.statusData = {};
        this.messageQueue = [];
    }
    toCacheJSON() {
        return { id: this.id, address: TARGET_ADDRESS, name: this.name, leds: 1 };
    }
}

export function DiscoveryService() {
    this.IconUrl = "";
    this.PollInterval = 5000;
    this.lastPollTime = 0;
    this.controller = null;

    this.Initialize = function () {
        this.controller = new H6001Controller();
        if (!service.hasController(CONTROLLER_ID)) {
            service.addController(this.controller);
            service.announceController(this.controller);
            service.log(`Announced ${this.controller.name} (${TARGET_ADDRESS})`);
        }
    };

    this.Update = function () {
        if (!this.controller) this.Initialize();
        if (!service.hasController(CONTROLLER_ID)) {
            service.addController(this.controller);
            service.announceController(this.controller);
        }
    };
}

let socket;
let lastColor = "";
let lastFrameAt = 0;

export function Initialize() {
    socket = udp.createSocket();
    device.setName(controller.device.getName());
    device.setSize([1, 1]);
    device.setControllableLeds(["H6001 Bulb"], [[0, 0]]);
    device.log(`Govee H6001 add-on ready for ${TARGET_ADDRESS}; bridge must be running.`);
}

export function Render() {
    if (enableOutput === "Disabled" || !socket) return;
    const now = Date.now();
    const delay = Number.parseInt(frameDelay, 10) || 50;
    if (now - lastFrameAt < delay) return;

    const sampled = device.color(0, 0);
    if (!sampled || sampled.length < 3) return;
    const rgb = sampled.slice(0, 3).map((channel) =>
        Math.max(0, Math.min(255, Math.round(channel)))
    );
    const key = rgb.join(",");
    if (key === lastColor) return;

    socket.write(JSON.stringify({ command: "color", rgb }), BRIDGE_HOST, BRIDGE_PORT);
    device.log(`Sent color to BLE bridge: RGB ${key}`);
    lastColor = key;
    lastFrameAt = now;
}

export function Shutdown() {
    if (socket) socket.close();
    socket = null;
}
