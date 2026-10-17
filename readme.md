# Hermes Hook

Hermes Hook ist eine Linux-Desktopanwendung auf Basis von Python und PyQt6 zum Untersuchen und Manipulieren des Speichers laufender Prozesse, etwa von Spielen. Das Projekt ist in Entwicklung.

## Funktionen

- **Prozessauswahl** mit Kommandozeile und Arbeitsverzeichnis.
- **Speicher-Scan** nach `int8`, `int16`, `int32`, `int64`, deren vorzeichenlosen Varianten `uint8` bis `uint64` sowie `float` und `double`: exakter Wert, größer/kleiner, Bereich, unbekannter Startwert sowie Folge-Scans auf verändert, unverändert, vergrößert oder verkleinert und auf „vergrößert/verkleinert/verändert um X“. Exakte und Um-X-Scans akzeptieren eine Toleranz (±); `float`- und `double`-Werte lassen sich exakt, gerundet oder abgeschnitten auf die eingegebenen Nachkommastellen vergleichen (`12.3` gerundet findet 12,25 bis 12,35). „Automatisch raten“ probiert `int32`, `int64`, `float` und `double`; kleinere Typen passen fast überall und müssen ausdrücklich gewählt werden.
- **Gemerkte Adressen** mit Live-Werten; Werte lassen sich direkt in den Prozess schreiben oder einfrieren.
- **Schreibüberwachung**: Ein Hardware-Watchpoint zeigt, welche Codestellen eine Adresse beschreiben.
- **Pointer-Suche**: Findet Pointer-Chains, über die gemerkte Adressen und Hooks den Wert auch nach einem Neustart des Programms wiederfinden.
- **Hooks**: An einer gefundenen Codestelle wird jeder Schreibzugriff abgefangen und der geschriebene Wert manipuliert, z. B. mit einem Multiplikator, einem festen Wert, Grenzen oder einer eigenen JavaScript-Funktion. Hooks werden pro Programm gespeichert und beim nächsten Start wieder installiert.
- **Hook-Templates**: Manipulationen werden als JavaScript-Funktionen mit Parametern in der Datenbank gepflegt und lassen sich ohne Spielprozess testen.
- **Hook-Debug**: Ein Debug-Fenster protokolliert jeden Aufruf eines Hooks samt Werten, Aufrufer, Ausgaben und Fehlern.

### Einfrieren oder Hook?

**❄ Einfrieren** hält den aktuellen Wert einer gemerkten Adresse fest: Hermes schreibt ihn alle 100 ms zurück. Erneutes Klicken gibt ihn frei; **✎ Wert schreiben** ändert den eingefrorenen Wert. Bei Einträgen mit Pointer-Chain oder statischer Adresse wird der Zustand gespeichert und gilt nach einem Neustart des Programms weiter. Einträge ohne Chain werden beim erneuten Laden freigegeben, weil ihre Adresse dann nicht mehr stimmen muss. Einfrieren braucht keine Schreibüberwachung, kann aber kurz flackern, weil das Spiel den Wert zwischen zwei Durchläufen ändern darf. Ein Hook mit dem Template „Fester Wert“ greift dagegen direkt an der Schreibinstruktion.

## Voraussetzungen

- GNU/Linux auf x86-64
- Python 3.11 oder neuer
- `venv` und `pip` für die ausgewählte Python-Version
- Eine grafische Sitzung mit X11 oder Wayland für die Qt-Oberfläche

Unter Debian oder Ubuntu können Python und die Werkzeuge bei Bedarf über die Distribution installiert werden:

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip
```

Die Python-Abhängigkeiten, einschließlich PyQt6, werden anschließend über `pip` aus `pyproject.toml` installiert. Wenn Qt beim Start ein fehlendes Plattform-Plugin meldet, müssen die Qt-/XCB-Laufzeitpakete der jeweiligen Distribution ergänzt werden.

## Installation

Im Verzeichnis des Projekts:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Die Editable-Installation richtet auch den Konsolenbefehl `hermes` ein.

## Start

Mit aktivierter virtueller Umgebung:

```bash
hermes
```

Alternativ lässt sich die Anwendung direkt aus dem Projektverzeichnis starten:

```bash
./.venv/bin/python main.py
```

Die Sprache wird anhand der System-Locale auf Deutsch oder Englisch gesetzt. Sie lässt sich beim Start explizit wählen:

```bash
HERMES_LANGUAGE=en hermes
HERMES_LANGUAGE=de hermes
```

Beim ersten Start wird die SQLite-Datenbank unter `~/.local/share/hermes-hook/hermes.db` angelegt (bzw. unter `$XDG_DATA_HOME/hermes-hook/`). Mit `HERMES_DB=/pfad/zur/datei.db` lässt sich eine andere Datei verwenden. Gibt es dort noch keine Datenbank, aber eine aus früheren Versionen unter `data/hermes.db` im Projektverzeichnis, wird diese einmalig kopiert; das Original bleibt erhalten. Für die Prozessliste werden nur Prozesse angezeigt, auf deren Informationen der aktuelle Benutzer zugreifen darf. Beendet sich der angehängte Prozess, setzt Hermes das Fenster innerhalb einer Sekunde zurück: Scans, Überwachung, Einfrieren und Hooks werden gestoppt, gespeicherte Adressen und Hooks bleiben erhalten und werden beim nächsten Anhängen wieder geladen.

## Linux-Rechte für Speicherzugriffe

Scans und Schreibzugriffe nutzen `/proc/<pid>/mem`, Schreibüberwachung und Hooks hängen sich über [Frida](https://frida.re) an den Zielprozess. Beides unterliegt den Linux-Schutzmechanismen, insbesondere Yama (`kernel.yama.ptrace_scope`):

- `0`: Prozesse desselben Benutzers dürfen untersucht werden.
- `1` (Standard vieler Distributionen): nur eigene Kindprozesse. Hermes kann dann auf ein separat gestartetes Spiel nicht zugreifen.

Starte die Anwendung nicht vorsorglich als root und lockere `ptrace_scope` nicht dauerhaft systemweit. Für eine Testsitzung genügt eine vorübergehende Änderung, die beim nächsten Neustart verfällt:

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

## Beispiel: Gold-Multiplikator für das Test-Spiel

`test_app/dummy_game.c` ist ein minimales GTK-Spiel mit Buttons für „Gold +100“, „Gold -100“, „New game“ sowie Mana, Erfahrung und Schatzsuche zum Testen unscharfer Scans (siehe [unten](#unscharfe-scans-toleranz-und-gerundete-floats)). Wie in echten Spielen liegt der Spieler auf dem Heap hinter Zeigern (`g_game → player → gold`), seine Adresse ändert sich also bei jedem Start und bei jedem „New game“. Das Spiel gibt seine PID, die Pointer-Chain und die aktuelle Adresse des Golds aus, sodass sich die Ergebnisse von Scanner und Pointer-Suche prüfen lassen. Es dürfen mehrere Instanzen gleichzeitig laufen.

```bash
mkdir -p build
gcc -O2 test_app/dummy_game.c -o build/dummy_game.exe $(pkg-config --cflags --libs gtk+-3.0)
./build/dummy_game.exe
```

Benötigt werden `gcc` und die GTK-3-Entwicklerpakete (`gtk3-devel` unter Fedora, `libgtk-3-dev` unter Debian/Ubuntu).

1. In Hermes **Prozess auswählen** → `dummy_game.exe`.
2. **Erster Scan** nach dem angezeigten Goldwert (`int32`), im Spiel Gold ändern, **Nächster Scan** mit dem neuen Wert, bis eine Adresse übrig bleibt. Mit **+ Merken** in die gemerkten Adressen übernehmen und eine Beschreibung wie „Gold“ vergeben.
3. Rechtsklick auf die Adresse → **Schreibzugriffe auf diese Adresse überwachen …**
4. Im Spiel **Gold +100** klicken. Im Überwachungsfenster erscheint die Codestelle, die das Gold erhöht (z. B. `dummy_game+0x…`). „Gold -100“ erzeugt eine zweite, eigene Zeile.
5. Die Zeile der Erhöhung markieren → **Hook erstellen …**, Template **Multiplikator**, Faktor `10`, „Nur Erhöhungen“.
6. Der Hook erscheint im Tab **Hooks**. Jeder Klick auf „Gold +100“ bringt jetzt 1000 Gold; die Spalte „Letzte“ zeigt alt → vom Spiel geschrieben ⇒ gespeichert.

Über den Tab **Hooks** lassen sich Hooks bearbeiten, pausieren, debuggen und entfernen. Beim Entfernen, beim Prozesswechsel und beim Beenden von Hermes werden die ursprünglichen Bytes wiederhergestellt.

### Unscharfe Scans: Toleranz und gerundete Floats

Das Spiel zeigt Mana (`float`) mit einer und Erfahrung (`double`) ohne Nachkommastelle an. Mana regeneriert jede Sekunde um 0,13, ein Zauber kostet 7,5; ein Kampf bringt 10–15 XP mit Nachkommastellen, eine Schatzsuche 20–30 Gold. Die Adressen gibt das Spiel bei jeder Runde aus.

- **Mana**: Datentyp `float`, **Exakter Wert** mit dem angezeigten Wert (z. B. `20.1`) und Float-Vergleich **Gerundet**; mit „Exakt“ findet sich die Adresse kaum, weil der gespeicherte Wert z. B. 20,13 ist. Dann **Cast spell** klicken und **Verkleinert um …** `7.5` mit Toleranz `0.5` scannen – die Regeneration zwischen den Scans würde einen Vergleich ohne Toleranz scheitern lassen.
- **Erfahrung**: Datentyp `double`, **Exakter Wert** mit der angezeigten Zahl und **Gerundet**, nach **Fight** **Vergrößert um …** `12` mit Toleranz `3`.
- **Gold**: Nach **Find treasure** **Vergrößert um …** `25` mit Toleranz `5` (`int32`).

### Hooks nach einem Neustart

Hooks werden pro Programm gespeichert, und zwar als Modul + Offset der Schreibinstruktion. Wählt man das Programm nach einem Neustart erneut aus, installiert Hermes die gespeicherten Hooks automatisch. Haben sich die Bytes an dieser Stelle geändert, etwa nach einem Update des Programms, wird der Hook nicht installiert. Liegt die Schreibinstruktion in keinem Modul (z. B. JIT-Code), kann der Hook nicht gespeichert werden.

Nach einem Neustart ist die ursprüngliche Zieladresse nicht mehr gültig. Woran der Hook die richtigen Schreibzugriffe erkennt, hängt davon ab, wie er angelegt wurde:

- **Mit Pointer-Chain** (siehe unten): Der Hook löst die Chain bei jedem Schreibzugriff auf und greift nur bei der Adresse, zu der sie gerade führt. Das ist die zuverlässigste Variante.
- **Ohne Pointer-Chain**: Der Hook merkt sich während der Laufzeit die Aufrufer, also die Rücksprungadressen auf dem Stack, die die Zieladresse beschreiben. Nach einem Neustart greift er nur bei Schreibzugriffen dieser Aufrufer. Wie viele Rücksprungadressen einen Aufrufer kennzeichnen, legt **Aufrufer-Ebenen** im Hook-Dialog fest (Standard `1`, `aus` lernt keine Aufrufer). Mehr Ebenen helfen, wenn Spieler und andere Objekte über dieselbe Hilfsfunktion geändert werden. Schreibt ein gelernter Aufrufer auch andere Adressen, weist Hermes darauf hin.

### Gold nach einem Neustart wiederfinden (Pointer-Suche)

Gemerkte Adressen werden pro Programm gespeichert. Damit „Gold“ nach einem Neustart des Spiels wieder stimmt, braucht der Eintrag eine Pointer-Chain:

1. Rechtsklick auf „Gold“ → **Pointer suchen …** → **Neue Suche**. Gefunden wird u. a. `dummy_game+0x…  → 0x28 → 0x0` (`g_game` → Spieler → Gold), dazu einige Zufallstreffer.
2. Im Spiel **New game** klicken (oder das Spiel neu starten) und das Gold im Scanner neu suchen – der Pointer-Dialog kann dabei offen bleiben. Das gefundene Ergebnis im Scanner markieren, im Pointer-Dialog **Aus Scan übernehmen** und **Mit dieser Adresse filtern**. Zufallstreffer, die nur durch die Anordnung im Speicher passten, fallen dabei heraus; ein reiner Neustart reicht dafür oft nicht, weil die Anordnung beim Start gleich bleibt.
3. Die verbleibende Chain auswählen → **Pointer verwenden**. Ab jetzt zeigt die Adressliste das Gold nach jedem Neustart und nach jedem „New game“ automatisch richtig an.
4. Wird die Schreibüberwachung aus diesem Eintrag gestartet, bekommt ein neuer Hook die Chain als Ziel: Er folgt dem Spieler auch nach „New game“ und nach einem Neustart.

Liegt eine Adresse im statischen Speicher des Programms (eine globale Variable), speichert Hermes sie beim Merken direkt als Modul + Offset; eine Pointer-Suche ist dann nicht nötig, und der Pointer-Dialog weist darauf hin.

### Wie ein Hook funktioniert

Die Schreibüberwachung meldet die Instruktion *nach* dem Schreibzugriff (Hardware-Watchpoints lösen erst danach aus). Hermes bestimmt daraus die eigentliche Schreibinstruktion und ersetzt sie durch einen Sprung in eine Code-Cave:

```
before(regs)       alten Wert sichern, falls die Instruktion wirklich die Zieladresse beschreibt
<Schreibinstruktion, relokiert>
after(regs)        Manipulation aufrufen und das Ergebnis speichern
<restliche relokierte Bytes>
jmp zurück
```

Weil Hermes nur mit altem und neuem Wert rechnet, ist egal, ob das Spiel `add [mem], reg`, `mov [mem], reg` oder `sub` verwendet. Wird dieselbe Instruktion auch für andere Objekte genutzt (z. B. das Gold von Gegnern), greift der Hook nur beim Ziel: bei der überwachten Adresse, der Adresse seiner Pointer-Chain oder, nach einem Neustart ohne Chain, bei den gelernten Aufrufern. Mehrere Hooks können dieselbe Schreibinstruktion nutzen; sie laufen in der Reihenfolge, in der sie angelegt wurden.

Einschränkungen:

- Nur x86-64-Prozesse.
- Die Schreibinstruktion und die Bytes bis zur Länge eines Sprungs (5 Bytes) werden überschrieben. Springt anderer Code mitten in diesen Bereich, stürzt das Ziel ab; dasselbe Risiko haben Frida-Hooks und die Code-Injection von Cheat Engine.
- Bricht die Verbindung zu Hermes unerwartet ab, während ein Hook aktiv ist, kann das Ziel abstürzen.
- `int64`- und `uint64`-Werte sind in Manipulationen nur bis 2^53 exakt.

## Hook-Templates und eigene Manipulationen

Eine Manipulation ist eine JavaScript-Funktion, die im Zielprozess nach jedem Schreibzugriff läuft. Sie erhält ein `ctx`-Objekt und gibt den zu speichernden Wert zurück; `undefined` oder `null` behält den Wert des Spiels. Ganzzahlige Ergebnisse werden abgeschnitten und auf den Wertebereich des Typs begrenzt.

| Feld | Bedeutung |
| --- | --- |
| `ctx.old`, `ctx.new`, `ctx.delta` | Wert vor und nach dem Schreibzugriff, Differenz |
| `ctx.params` | Parameter des Templates mit den Werten des Hooks |
| `ctx.state` | Objekt, das zwischen Aufrufen erhalten bleibt (z. B. für Zähler) |
| `ctx.valueType`, `ctx.address`, `ctx.writePc`, `ctx.threadId`, `ctx.hookId` | Kontext des Schreibzugriffs |
| `ctx.log(…)` | Ausgabe im Debug-Fenster; ohne Debug-Modus wirkungslos |

### Templates pflegen

Manipulationen werden als **Hook-Templates** in der Datenbank gespeichert und über die Schaltfläche **⧉ Hook-Templates** verwaltet. Beim ersten Start legt Hermes die Beispiele **Multiplikator**, **Fester Wert** und **Begrenzen** in der Sprache der Oberfläche an; sie lassen sich wie eigene Templates ändern oder löschen.

Ein Template besteht aus Name, optionaler Beschreibung, der JavaScript-Funktion und Parametern. Für jeden Parameter werden Name (im Skript `ctx.params.<Name>`), Bezeichnung, Typ (**Zahl** oder **Auswahl**), Standardwert und ob er optional ist festgelegt; Auswahlwerte werden als `wert=Bezeichnung; wert=Bezeichnung` angegeben. Aus den Parametern erzeugt der Hook-Dialog das Eingabeformular. Beispiel für ein Template mit dem Zahl-Parameter `threshold`:

```js
ctx => ctx.delta >= ctx.params.threshold ? ctx.new : ctx.old
```

Mit **▶ Testen** läuft die Funktion ohne Spielprozess: Werttyp, alten Wert (`ctx.old`) und vom Spiel geschriebenen Wert (`ctx.new`) eingeben und ausführen. Angezeigt werden der gespeicherte Wert, Ausgaben von `ctx.log`/`console.log` und Fehler. `ctx.state` bleibt zwischen den Testläufen erhalten, bis er zurückgesetzt wird. Der Test läuft in Qts JavaScript-Engine statt in Frida, Frida-APIs wie `ptr()` oder `Memory` stehen dort also nicht zur Verfügung.

Ein Hook speichert eine **Kopie** des Templates, aus dem er angelegt wurde. Spätere Änderungen am Template wirken sich deshalb nicht auf bestehende Hooks aus. Im Dialog **Bearbeiten** weist Hermes auf ein geändertes Template hin und bietet **Geändertes Template übernehmen** an. Wird das Template gelöscht, arbeitet der Hook mit seiner Kopie weiter.

### Hooks debuggen

Mit **◉ Debug** im Tab **Hooks** wird der ausgewählte Hook in den Debug-Modus versetzt. Das Debug-Fenster protokolliert dann jeden Aufruf mit Zeit, Thread, Adresse, Aufrufer, altem Wert, Spielwert, Ergebnis, `ctx.log`-Ausgaben und Fehlern samt Stack. Auch Schreibzugriffe, die der Hook überspringt, erscheinen mit Begründung, etwa „schreibt eine andere Adresse“ oder „Aufrufer nicht gelernt“. Der Debug-Modus kostet Zeit im Spiel; bei sehr vielen Aufrufen verwirft der Agent Einträge und zählt sie nur. Das Schließen des Fensters beendet den Debug-Modus.

### Beispiel-Templates im Code

Die Beispiel-Templates stammen aus Unterklassen von `ValueTransform` in `hermes/hooks/transforms.py`, deren `kind` in `EXAMPLE_TEMPLATE_KINDS` steht. Fehlt beim Start ein Beispiel (gleicher Seed-Key oder Name), legt Hermes es an. Name, Beschreibung und Parameterbezeichnungen kommen aus `hermes/ui/localization/*.json` unter `transform.<kind>`, `transform.<kind>.description`, `transform.<kind>.<parameter>` und `transform.choice.<wert>`; fehlen sie, werden `title`, der Parametername bzw. der Auswahlwert verwendet. Für eigene Manipulationen ist das nicht nötig: Sie werden direkt als Template in der Oberfläche angelegt.

## Tests

```bash
./.venv/bin/python -m pytest
```

Die Hook-Tests kompilieren ein kleines C-Programm und hängen sich mit Frida daran. Sie werden übersprungen, wenn `gcc` fehlt oder `kernel.yama.ptrace_scope` nicht `0` ist.
