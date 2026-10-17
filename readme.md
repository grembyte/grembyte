# Hermes Hook

Hermes Hook ist eine Linux-Desktopanwendung auf Basis von Python und PyQt6 zum Untersuchen und Manipulieren des Speichers laufender Prozesse, etwa von Spielen. Das Projekt ist in Entwicklung.

## Funktionen

- **Prozessauswahl** mit Kommandozeile und Arbeitsverzeichnis.
- **Speicher-Scan** nach `int32`, `int64`, `float` und `double`: exakter Wert, größer/kleiner, Bereich, unbekannter Startwert sowie Folge-Scans auf verändert, unverändert, vergrößert oder verkleinert.
- **Gemerkte Adressen** mit Live-Werten; Werte lassen sich direkt in den Prozess schreiben.
- **Schreibüberwachung**: Ein Hardware-Watchpoint zeigt, welche Codestellen eine Adresse beschreiben.
- **Hooks**: An einer gefundenen Codestelle wird jeder Schreibzugriff abgefangen und der geschriebene Wert manipuliert, z. B. mit einem Multiplikator, einem festen Wert, Grenzen oder einer eigenen JavaScript-Funktion.

Noch nicht umgesetzt ist das Einfrieren gemerkter Adressen über die Schaltfläche „Einfrieren“; ein Hook mit „Fester Wert“ erfüllt denselben Zweck an der Codestelle.

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

Beim ersten Start wird die SQLite-Datenbank unter `data/hermes.db` angelegt. Für die Prozessliste werden nur Prozesse angezeigt, auf deren Informationen der aktuelle Benutzer zugreifen darf.

## Linux-Rechte für Speicherzugriffe

Scans und Schreibzugriffe nutzen `/proc/<pid>/mem`, Schreibüberwachung und Hooks hängen sich über [Frida](https://frida.re) an den Zielprozess. Beides unterliegt den Linux-Schutzmechanismen, insbesondere Yama (`kernel.yama.ptrace_scope`):

- `0`: Prozesse desselben Benutzers dürfen untersucht werden.
- `1` (Standard vieler Distributionen): nur eigene Kindprozesse. Hermes kann dann auf ein separat gestartetes Spiel nicht zugreifen.

Starte die Anwendung nicht vorsorglich als root und lockere `ptrace_scope` nicht dauerhaft systemweit. Für eine Testsitzung genügt eine vorübergehende Änderung, die beim nächsten Neustart verfällt:

```bash
sudo sysctl kernel.yama.ptrace_scope=0
```

## Beispiel: Gold-Multiplikator für das Test-Spiel

`test_app/dummy_game.c` ist ein minimales GTK-Spiel mit Buttons für „Gold +100“, „Gold -100“ und „New game“. Wie in echten Spielen liegt der Spieler auf dem Heap hinter Zeigern (`g_game → player → gold`), seine Adresse ändert sich also bei jedem Start und bei jedem „New game“. Das Spiel gibt seine PID, die Pointer-Chain und die aktuelle Adresse des Golds aus, sodass sich die Ergebnisse von Scanner und Pointer-Suche prüfen lassen. Es dürfen mehrere Instanzen gleichzeitig laufen.

```bash
mkdir -p build
gcc -O2 test_app/dummy_game.c -o build/dummy_game.exe $(pkg-config --cflags --libs gtk+-3.0)
./build/dummy_game
```

Benötigt werden `gcc` und die GTK-3-Entwicklerpakete (`gtk3-devel` unter Fedora, `libgtk-3-dev` unter Debian/Ubuntu).

1. In Hermes **Prozess auswählen** → `dummy_game`.
2. **Erster Scan** nach dem angezeigten Goldwert (`int32`), im Spiel Gold ändern, **Nächster Scan** mit dem neuen Wert, bis eine Adresse übrig bleibt. Mit **+ Merken** in die gemerkten Adressen übernehmen und eine Beschreibung wie „Gold“ vergeben.
3. Rechtsklick auf die Adresse → **Schreibzugriffe auf diese Adresse überwachen …**
4. Im Spiel **Gold +100** klicken. Im Überwachungsfenster erscheint die Codestelle, die das Gold erhöht (z. B. `dummy_game+0x…`). „Gold -100“ erzeugt eine zweite, eigene Zeile.
5. Die Zeile der Erhöhung markieren → **Hook erstellen …**, Manipulation **Multiplikator**, Faktor `10`, „Nur Erhöhungen“.
6. Der Hook erscheint im Tab **Hooks**. Jeder Klick auf „Gold +100“ bringt jetzt 1000 Gold; die Spalte „Letzte“ zeigt alt → vom Spiel geschrieben ⇒ gespeichert.

Über den Tab **Hooks** lassen sich Hooks bearbeiten, pausieren und entfernen. Beim Entfernen, beim Prozesswechsel und beim Beenden von Hermes werden die ursprünglichen Bytes wiederhergestellt.

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

Weil Hermes nur mit altem und neuem Wert rechnet, ist egal, ob das Spiel `add [mem], reg`, `mov [mem], reg` oder `sub` verwendet. Wird dieselbe Instruktion auch für andere Objekte genutzt (z. B. das Gold von Gegnern), greift der Hook nur bei der überwachten Adresse.

Einschränkungen:

- Nur x86-64-Prozesse.
- Die Schreibinstruktion und die Bytes bis zur Länge eines Sprungs (5 Bytes) werden überschrieben. Springt anderer Code mitten in diesen Bereich, stürzt das Ziel ab; dasselbe Risiko haben Frida-Hooks und die Code-Injection von Cheat Engine.
- Bricht die Verbindung zu Hermes unerwartet ab, während ein Hook aktiv ist, kann das Ziel abstürzen.
- `int64`-Werte sind in Manipulationen nur bis 2^53 exakt.

## Eigene Manipulationen

Eine Manipulation ist eine JavaScript-Funktion, die im Zielprozess nach jedem Schreibzugriff läuft. Sie erhält ein `ctx`-Objekt und gibt den zu speichernden Wert zurück; `undefined` behält den Wert des Spiels.

| Feld | Bedeutung |
| --- | --- |
| `ctx.old`, `ctx.new`, `ctx.delta` | Wert vor und nach dem Schreibzugriff, Differenz |
| `ctx.params` | Parameter der Manipulation |
| `ctx.state` | Objekt, das zwischen Aufrufen erhalten bleibt (z. B. für Zähler) |
| `ctx.valueType`, `ctx.address`, `ctx.writePc`, `ctx.threadId`, `ctx.hookId` | Kontext des Schreibzugriffs |

Für einmalige Experimente gibt es in der Oberfläche **Eigenes Skript (JavaScript)**, etwa:

```js
ctx => ctx.delta > 0 ? ctx.old + ctx.delta * 3 : undefined
```

Wiederverwendbare Manipulationen werden in Python als Unterklasse von `ValueTransform` in `hermes/hooks/transforms.py` (oder einem eigenen, beim Start importierten Modul) registriert. Über `parameters` erzeugt die Oberfläche das Eingabeformular automatisch:

```python
from hermes.hooks.transforms import TransformParameter, ValueTransform, register_transform


@register_transform
class OnlyBigGains(ValueTransform):
    """Gewinne unterhalb einer Schwelle verwerfen."""

    kind = "only-big-gains"
    title = "Only big gains"
    source = "ctx => ctx.delta >= ctx.params.threshold ? ctx.new : ctx.old"
    parameters = (TransformParameter("threshold", default=50),)

    def __init__(self, threshold):
        super().__init__(threshold=threshold)
```

Übersetzte Beschriftungen kommen aus `hermes/ui/localization/*.json` unter `transform.<kind>` und `transform.<kind>.<parameter>`; fehlen sie, werden `title` und der Parametername verwendet.

## Tests

```bash
./.venv/bin/python -m pytest
```

Die Hook-Tests kompilieren ein kleines C-Programm und hängen sich mit Frida daran. Sie werden übersprungen, wenn `gcc` fehlt oder `kernel.yama.ptrace_scope` nicht `0` ist.
