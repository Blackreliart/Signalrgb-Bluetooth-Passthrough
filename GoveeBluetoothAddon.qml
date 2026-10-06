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
            text: "Starte zuerst bridge.py. Nach der Bluetooth-Verbindung meldet das Add-on die H6001 als eigenes Gerät bei SignalRGB an. Dort erhält sie eine eigene Geräteseite mit Beleuchtungs- und Einstellungen-Tab."
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
            text: "Das Add-on prüft den Bridge-Status lokal über UDP. Die Bridge muss laufen und mit Minger_H6001_0BF9 verbunden sein."
        }
    }
}
