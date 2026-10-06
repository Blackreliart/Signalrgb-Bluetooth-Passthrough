import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    width: 620
    height: 250

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 18
        spacing: 12

        Label {
            text: "Govee Bluetooth Bridge"
            font.pixelSize: 20
            font.bold: true
        }

        Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: "Starte zuerst bridge.py. Die Bridge sucht deine H6001 und verbindet sie per Bluetooth. Dieses Add-on sendet die SignalRGB-Farbe lokal an die Bridge (UDP 127.0.0.1:8765)."
        }

        RowLayout {
            Layout.fillWidth: true
            Label { text: "Bridge-Adresse:" }
            Label { text: "127.0.0.1:8765"; font.bold: true }
        }

        Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: "#d9a441"
            text: "Hinweis: Die BLE-Suche und Geräteauswahl laufen im ersten Prototyp in der Bridge-Konsole. Eine direkte Steuerung ohne Bridge ist in SignalRGBs dokumentierter Schnittstelle nicht verfügbar."
        }
    }
}
