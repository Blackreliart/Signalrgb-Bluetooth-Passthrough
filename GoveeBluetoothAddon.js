// SignalRGB Add-on for one fixed Govee H6001 BLE bulb.
// DiscoveryService announces a virtual network controller to SignalRGB;
// Initialize/Render sample its one LED and send colors to the local BLE bridge.
import udp from "@SignalRGB/udp";

const TARGET_ADDRESS = "A4:C1:38:75:0B:F9";
const BRIDGE_HOST = "127.0.0.1";
const BRIDGE_PORT = 8765;

export function Name() { return "Govee H6001 Bluetooth"; }
export function Version() { return "0.3.3"; }
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

export function DiscoveryService() {
    // SignalRGB evaluates DiscoveryService callbacks in a service context. Keep
    // identifiers inside that context instead of relying on module constants.
    const serviceTargetAddress = "A4:C1:38:75:0B:F9";
    const serviceControllerId = "govee-h6001-a4c138750bf9";
    this.IconUrl = "";
    this.PollInterval = 1000;
    this.lastPollTime = 0;
    this.controller = null;
    this.connected = false;
    this.socket = null;

    // Keep the factory inside DiscoveryService. SignalRGB evaluates service
    // callbacks in its own context, so an outer class declaration is not visible.
    this.makeController = function () {
        const goveeDevice = {
            id: serviceControllerId,
            address: serviceTargetAddress,
            name: "Minger H6001 (Bluetooth)",
            leds: 1,
            type: 3,
            split: 1,
            getName: function () { return this.name; }
        };
        return {
            device: goveeDevice,
            id: serviceControllerId,
            name: goveeDevice.name,
            changed: false,
            connected: true,
            statusData: {},
            messageQueue: [],
            toCacheJSON: function () {
                return { id: serviceControllerId, address: serviceTargetAddress, name: goveeDevice.name, leds: 1, type: 3, split: 1 };
            }
        };
    };

    this.Initialize = function () {
        // Use the same bound UDP socket for requests and replies. Otherwise the
        // bridge answers the ephemeral source port and SignalRGB never sees status.
        this.socket = udp.createSocket();
        this.socket.on("message", this.handleBridgeMessage.bind(this));
        this.socket.on("error", (errorId, errorMessage) =>
            service.log(`Bridge UDP error ${errorId}: ${errorMessage || ""}`));
        this.socket.bind(8766);
        service.log("Govee add-on status socket bound to UDP 8766");
        this.lastPollTime = 0;
    };

    this.Update = function () {
        const now = Date.now();
        if (!this.socket || now - this.lastPollTime < this.PollInterval) return;
        this.lastPollTime = now;
        this.socket.write(JSON.stringify({ command: "status" }), BRIDGE_HOST, BRIDGE_PORT);
    };

    this.handleBridgeMessage = function (packet) {
        let message;
        try {
            message = JSON.parse(packet.data);
        } catch (error) {
            service.log(`Invalid bridge response: ${error}`);
            return;
        }
        service.log(`Bridge status received: connected=${message.connected}`);
        if (message.type !== "status") return;

        if (message.connected && !this.connected) {
            this.controller = this.makeController();
            service.log("Registering H6001 controller with SignalRGB");
            service.addController(this.controller);
            service.announceController(this.controller);
            this.connected = true;
            service.log(`H6001 connected and announced: ${serviceTargetAddress}`);
        } else if (!message.connected && this.connected) {
            if (this.controller) service.removeController(this.controller);
            this.controller = null;
            this.connected = false;
            service.log("H6001 disconnected; removed SignalRGB device.");
        }
    };

    this.Shutdown = function () {
        if (this.socket) this.socket.close();
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
