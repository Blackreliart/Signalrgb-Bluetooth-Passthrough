# Govee H6001 Bluetooth Bridge für SignalRGB

Die Bridge verbindet ausschließlich die Govee/Minger H6001 mit der Bluetooth-Adresse `A4:C1:38:75:0B:F9`. Sie läuft als Windows-Anwendung mit Symbol im Infobereich. SignalRGB sendet Farben über das lokale UDP-Protokoll an die Bridge; ein zusätzliches Konsolenfenster ist nicht nötig.

Beim Programmstart ist SignalRGB-Sync ausgeschaltet und die Lampe startet mit warmem Weiß (2700 K). Der Regler bildet den Bereich von 2700 bis 6500 K mathematisch in Mireds (Kehrwert der Kelvin-Temperatur) ab. Gesendet wird ein H6001-Paket mit aktiviertem WW-Modus; die RGB-Kanäle sind dabei nicht ausgewählt. Beim Ausschalten von SignalRGB-Sync wird der aktuelle Kelvinwert erneut im WW-Modus gesendet und ersetzt den vorherigen RGB-Farbton.

Zusätzlich läuft ein lokaler Webserver für die Handy-Steuerung. Handy und PC müssen im selben WLAN sein. Öffne die im Bridge-Fenster angezeigte Adresse auf dem Handy und gib die dort angezeigte sechsstellige PIN ein. Die Weboberfläche bietet Ein/Aus, SignalRGB-Sync und den Temperaturregler. Der Server ist nur im lokalen Netzwerk gedacht und mit der Start-PIN geschützt.

Vor dem Beenden setzt die Bridge eine eingeschaltete Lampe auf 6500 K Kaltweiß im WW-Modus und trennt danach Bluetooth. Das geht über **Beenden · Lampe auf WW-Kaltweiß** im Fenster oder **Beenden · WW-Kaltweiß** im Tray-Menü. Über **Bluetooth neu verbinden** im Fenster oder auf der Handy-Webseite kannst du die Verbindung manuell neu aufbauen.

## Einrichten

1. Python 3.10 oder neuer installieren.
2. In diesem Ordner einmalig die Abhängigkeiten installieren:

   ```powershell
   py -m pip install -r requirements.txt
   ```

3. Bridge starten:

   ```powershell
   pythonw bridge.py
   ```

   Oder `Start_GoveeBridge.vbs` doppelklicken. Der Starter öffnet die Bridge ohne sichtbares Terminalfenster.
4. `GoveeBluetoothAddon.js` wie bisher in SignalRGB laden. Die Bridge muss laufen, bevor das Add-on das H6001-Gerät anmeldet.

## Bedienung

- **Lampe ein-/ausschalten** steuert die H6001 direkt.
- **SignalRGB-Sync** schaltet die RGB-Übertragung ein oder pausiert sie. Beim Pausieren verwendet die Lampe die eingestellte Weißtemperatur.
- Mit dem **Weißtemperatur-Regler** stellst du den Weißton zwischen 2700 K (warm) und 6500 K (kalt) ein. Beim Verstellen wird die SignalRGB-Synchronisierung pausiert; zum Fortsetzen den Haken wieder aktivieren.
- Das Fenster-Schließen blendet die Anwendung in den Infobereich neben der Uhr aus. Über das Tray-Symbol lässt sich das Fenster öffnen, die Lampe schalten oder die Bridge beenden.
- Die Handy-Oberfläche erreichst du über die angezeigte `http://...:8767`-Adresse. Falls Windows die Verbindung blockiert, muss Python im privaten Netzwerk durch die Firewall dürfen.
- **Bluetooth neu verbinden** trennt die bestehende BLE-Verbindung und startet die Suche nach genau der hinterlegten H6001-Adresse erneut.
- Das Schließen des Fensters blendet es nur in den Infobereich aus. Zum Beenden den neuen **Beenden**-Knopf oder das Tray-Menü verwenden; dabei wird zuerst WW-Kaltweiß gesetzt.

Die Bridge schreibt keine laufenden Statusmeldungen in ein Konsolenfenster. Das rotierende Protokoll liegt unter `%LOCALAPPDATA%\GoveeSignalRGB\bridge.log`.
