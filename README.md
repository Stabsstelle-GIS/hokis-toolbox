# hokis-toolbox
Misc. tools for ArcGIS+Terratwin (HOKIS)

## Installation

1. Daten aus Github-Repository herunterladen und lokal speichern
2. Im ArcGIS Pro Katalog Rechtsklick auf 'Toolboxen'->'Toolbox hinzufügen'
3. zu heruntergeladenen Daten navigieren und die Datei 'toolbox_webmap.pyt' auswählen; dieses erscheint dann im ArcGIS Pro Katalog unter 'Toolboxen'
4. 'toolbox_webmap.pyt' aufklappen und das Werkzeug mit Doppelklick öffnen

## Tools

### Werkzeug zum Austausch veralteter Layer-Referenzen in ArcGIS Enterprise WebMaps

Dieses Werkzeug ersetzt veraltete Layer-Referenzen in bestehenden WebMaps automatisiert durch einen neuen Ziel-Layer.

Dazu wird die JSON-Struktur der WebMap analysiert und der betroffene Layer abhängig vom Zieltyp kontrolliert ersetzt. Dabei werden sinnvolle Eigenschaften des ursprünglichen Layers möglichst erhalten.

Beim Ersetzen von Layern werden Eigenschaften nicht pauschal übernommen, sondern abgestuft behandelt:

SAFE_KEYS: typübergreifend unkritische Eigenschaften, die nach Möglichkeit erhalten bleiben
SAME_TYPE_KEYS: Eigenschaften, die nur bei gleichem Layer-Typ übernommen werden
TARGET_TYPE_KEYS: Eigenschaften, die gezielt für den jeweiligen Zieltyp übernommen werden

Problematische Inhalte werden bewusst nicht übernommen, um die Stabilität der WebMap nicht zu gefährden.

### Hintergrund

Nach Neuveröffentlichungen oder technischen Änderungen verweisen WebMaps oft noch auf alte Layer-Referenzen. Die manuelle Korrektur ist besonders bei GroupLayern, Unterlayern und verschachtelten Strukturen aufwendig und fehleranfällig.

Dieses Werkzeug automatisiert den Austausch solcher Referenzen und arbeitet dabei bewusst vorsichtig, um fehlerhafte Änderungen an WebMaps zu vermeiden.

### Ablauf

Das Werkzeug arbeitet direkt auf der JSON-Struktur von WebMaps und verwendet die aktive Portal-Anmeldung aus ArcGIS Pro.

1. Aktives Portal und aktuelle Anmeldung aus ArcGIS Pro übernehmen
2. Alle WebMaps des aktuell angemeldeten Portal-Benutzers suchen
3. Die operationalLayers jeder WebMap rekursiv durchlaufen
4. Treffer auf alte Layer-Referenzen erkennen
5. Betroffene Layer abhängig vom Zieltyp kontrolliert ersetzen
6. Sinnvolle Eigenschaften des ursprünglichen Layers übernehmen
7. GroupLayer-Konflikte und problematische Sonderfälle erkennen
8. WebMap im Dry Run nur prüfen oder bei Bedarf speichern
9. Ergebnisse, Konflikte und Laufzeit-Hinweise protokollieren
10. Unterstützte Layer-Typen

**Das Werkzeug unterstützt den Austausch auf folgende Zieltypen:**

1. Feature Layer
2. Map Image Layer
3. WMS
4. Vector Tile Layer
5. Tile Layer

Zusätzlich werden auch GroupLayer-Strukturen und verschachtelte Layer berücksichtigt.

### Eingabeparameter

Alte Layer-IDs
Liste der alten Layer-Item-IDs, die in WebMaps gesucht und ersetzt werden sollen.

Neue Layer-ID
Item-ID des neuen Ziel-Layers, der anstelle der alten Layer verwendet werden soll.

Dry Run
Steuert, ob Änderungen nur geprüft oder tatsächlich gespeichert werden.

True: keine Speicherung, nur Analyse und Ausgabe der geplanten Änderungen
False: geänderte WebMaps werden gespeichert
