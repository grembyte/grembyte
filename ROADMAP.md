# Roadmap und Ideen

Sammlung von Erweiterungen, die beim Finden von Adressen und Hook-Einstiegspunkten
helfen würden, und von offenen Punkten aus der Wine/Proton- und Unity-Arbeit.
Stand: Oktober 2026.

## Ausgangslage

Engine-unabhängig vorhanden:

- Wertsuche: exakt, Bereich, unbekannter Startwert, verändert/unverändert, größer/kleiner,
  Differenzen, unscharfe Floats
- Pointer-Scan für stabile Pointerketten
- Write-Watch: welche Instruktion eine Adresse schreibt, optional auch liest
- Hooks auf Schreibinstruktionen, mit Aufrufer-Lernen
- Speicheransicht, Disassembler, Einfrieren

Für Unity-Spiele mit Mono gibt es darüber hinaus Katalog, Trace (mit Vorauswahl-Filtern),
Methoden-Monitor und Methoden-Hooks über die Engine-Adapter-Schicht (`hermes/engines/`).

## Vorschläge, engine-unabhängig

Sortiert nach Nutzen im Verhältnis zum Aufwand.

### 1. Nativer Aufruf-Diff-Trace

Das native Gegenstück zum Mono-Trace und der größte Hebel. Fridas **Stalker** zeichnet
auf, welche Funktionen bzw. Basisblöcke ein Thread ausführt. Mit derselben Logik wie
beim Mono-Trace (Grundrauschen gegen Szenario) findet man die Funktion, die nur beim
Bezahlen oder bei Schaden läuft.

- Funktioniert ohne Metadaten, also für C++-Spiele, eigene Engines, Unreal, Godot, Source
- Wiederverwendbar: Trace-Dialog, `analyze`, Einstufung „neu/häufiger"
- Neu wäre nur der Agent (Stalker statt Mono-Hooks)
- Vorbild: Cheat Engine „Code Filter" / „Ultimap"
- Offen: Verhalten von Stalker unter Wine mit dem Windows-Gadget prüfen

**Stand (Prototyp, 19.10.2026):** Ansicht → „Nativer Trace …", unabhängig von einer Engine.
`hermes/features/native_trace.py` (Agent + `NativeTraceWorker`),
`hermes/ui/widgets/native_trace.py` (Fenster). Gemeinsame Sitzungslogik mit dem Mono-Trace
in `AgentWorker` (`hermes/features/trace.py`); `analyze` und die Einstufung sind dieselben.

- Stalker folgt den gewählten Threads mit `events: {call: true}` und `onCallSummary`;
  nicht gewählte Module werden per `Stalker.exclude` nativ ausgeführt (nur Aufrufe *in*
  sie zählen). Vorauswahl: Module aus dem Ordner des Spiels, Threads, die gerade laufen
  (CPU-Zeit oder Kontextwechsel in `/proc`).
- Funktionsnamen: Exporte, unter Linux zusätzlich Symbole (`DebugSymbol`), sonst `modul+0x…`.
- Verifiziert: Testspiel (C) und ein Python-Interpreter mit `libpython` (~6 Mio. Aufrufe/2 s,
  Szenario fand den JSON-Encoder). Noch nicht in einem echten Spiel getestet.
- Gelernt: Stalkers Queue fasst standardmäßig 16 384 Ereignisse pro Leerung und verwirft den
  Rest **stillschweigend**. Jetzt 131 072 bei 25 ms; ein voller Durchlauf wird als
  `saturated` gemeldet und im Fenster gewarnt.
- Gelernt: Ein verfolgter Thread, der in einem blockierenden Aufruf hängt, hält Stalkers
  Code fest; `script.unload()` wartet dann bis zu ~25 s (das Spiel überlebt). Daher werden
  untätige Threads nicht vorausgewählt, und „Verfolgung beenden" entlädt den Agenten nicht
  sofort. Ein neuer Trace bekommt einen frischen Agenten, weil Stalker Ausschlüsse nicht
  zurücknehmen kann.

Nächste Schritte:

- In einem echten nativen Spiel ausprobieren (Last, Spielbarkeit, Trefferqualität)
- Unter Wine/Proton über das Gadget testen (Fenster warnt bisher nur)
- Aufrufer: Die Zusammenfassung kennt nur Ziele. Für auffällige Funktionen könnte ein
  kurzer Interceptor-Lauf die Aufrufer nachliefern, oder ein CModule-`transform` zählt in C
  mit Aufrufstelle (auch genauer als die 25-ms-Reihenfolge)
- Anschluss an vorhandene Werkzeuge: Disassembler-Ansicht der Funktion, Hook auf die
  Funktion, später Byte-Signatur (Punkt 5)
- **Erledigt (19.10.2026):** Ergebnisse sortier- und filterbar (Name, Aufrufzahl, `%3`).
  Nativer Funktions-Monitor (`hermes/features/native_monitor.py`): Interceptor auf eine
  Funktion aus dem Trace oder per Adresse/Modul+Offset/Symbol; protokolliert Argumente,
  Ergebnis, Aufrufer und vergleicht den Speicher hinter Zeiger-Argumenten vor/nach dem
  Aufruf (bei C++-Methoden: welche Felder von `this` gesetzt werden). In Baldur's Gate
  (nativ, Linux) noch nicht ausprobiert.
- Threads, die während des Trace entstehen, werden nicht verfolgt

### 2. „Welche Adressen fasst diese Instruktion an?"

Umkehrung der Write-Watch: die gefundene Schreibinstruktion hooken und alle Adressen
sammeln, auf die sie zugreift, jeweils mit Registern und Aufrufer.

- Löst das Problem **geteilten Codes**: Dieselbe Instruktion verringert die HP von
  Spieler *und* Gegnern. Der Vergleich zeigt, welche Adresse dem Spieler gehört und woran
  der Code das unterscheidet (Register, Aufrufer, Team-Feld).
- Der Hook-Agent kann bereits Register lesen und Aufrufer-Signaturen bilden.

### 3. Strukturen sezieren und vergleichen

Steht eine Adresse fest, ist das umgebende Objekt interessant: Max-HP, Mana, Gold und
Team liegen meist daneben.

- **Strukturansicht:** Speicher ab Objektbeginn als Felder deuten (Pointer, Floats,
  Ints, Strings) mit Typvermutung
- **Instanzvergleich:** Spieler gegen Gegner oder zwei Zeitpunkte nebeneinander,
  Unterschiede hervorgehoben
- **Rückwärtssuche:** Wer zeigt auf dieses Objekt? Führt zum Basiszeiger und zu stabilen
  Pointerketten.

### 4. Einstiegspunkte über Strings, RTTI und Symbole

- **String-Referenzen:** Texte wie „Not enough gold" oder „GoldAmount" im Speicher
  finden, dann die Code-Stellen, die darauf verweisen. Das ergibt oft direkt die passende
  Funktion.
- **C++-RTTI:** Typnamen (MSVC `.?AV…@@`, Itanium `typeinfo`) führen zu vtables, also zu
  Klassennamen für Objekte im Speicher. Eine Adresse wird dann zu „`CPlayerStats`+0x48".
- **Symbole:** Native Linux-Spiele sind oft nicht vollständig gestrippt. Exportierte oder
  Debug-Symbole liefern Funktionsnamen.

### 5. Update-feste Hooks über Byte-Signaturen

Für nativen Code ein Muster (AOB) aus den Bytes um die Schreibinstruktion erzeugen, mit
Platzhaltern für Adressen und Offsets, und nach einem Spiel-Update automatisch neu
suchen. Für JIT-Code leistet das bereits die Methodenidentität; für native Spiele fehlt
es.

### 6. Weitere Engine-Adapter

Die Adapter-Schicht (`hermes/engines/base.py`, `registry.py`) ist dafür ausgelegt: ein
Unterpaket mit `EngineAdapter`-Unterklasse und Agent-Teil, der sich per
`registerEngine(...)` anmeldet, dazu ein Eintrag in `ENGINES`.

| Engine | Ansatz | Ergebnis |
|---|---|---|
| **Unreal** | Reflection über GNames/GObjects | alle Klassen, Properties mit Offsets, UFunctions; ähnlich mächtig wie Mono |
| **IL2CPP** (Unity ohne Mono) | `global-metadata.dat` + `il2cpp_*`-API | wie Mono, aber vorkompiliert; teilt das .NET-Modell des JS-Vertrags |
| **Godot** | ClassDB / Objekt-Properties | Szenenbaum und Variablen |
| **Lua-Engines** | `lua_*`-API hooken | Tabellen und Variablen des Skriptzustands |
| **RPG Maker MV/MZ, Ren'Py** | JavaScript- bzw. Python-Laufzeit | Spielvariablen direkt im Skriptzustand |

### 7. Kleinere Ergänzungen zur Suche

- **Gruppensuche:** mehrere Werte mit festen Abständen, z. B. HP 120 und Max-HP 150 vier
  Bytes weiter. Findet Strukturen sofort.
- **Spielstand-Diff:** zwei Spielstände vergleichen, um Werte und Layout zu erkennen, und
  dann im Speicher danach suchen.

### Empfehlung

**1 (Stalker-Diff-Trace)** und **2 (Zugriffe einer Instruktion)** bringen engine-unabhängig
am meisten und nutzen viel Vorhandenes. Bei den Adaptern wäre **Unreal** der nächste große
Schritt, **IL2CPP** der naheliegendste für Unity-Spiele ohne Mono.

## Offene Punkte aus der Wine/Proton-Arbeit

- **Methoden-Monitor und Methoden-Hooks unter Wine:** `hermes/features/monitor.py` ordnet
  Argumente nach der System-V-Aufrufkonvention zu (`rdi, rsi, …`). Unter Wine kompiliert
  Mono nach der Win64-Konvention (`rcx, rdx, r8, r9` bzw. `xmm0–3` nach Position,
  32 Byte Shadow-Space, andere Struct-Übergabe). Bis das in der Plattform-Schicht
  berücksichtigt ist, sind Argument- und Rückgabewerte dort unter Wine falsch.
- **Große Traces unter Wine:** Bei Zehntausenden Methoden fror das Spiel beim Kompilieren
  ein (Monos Garbage Collector hing in `SuspendThread`) oder stürzte beim Hooken in Fridas
  Code ab (Schreibzugriff auf den Header von `OPENGL32.dll` an `0x7BEB0000`). Umgangen
  durch Vorauswahl-Filter und eine Obergrenze (`WINE_TRACE_LIMIT`), nicht behoben.
  Kandidat für einen Bug-Report an Frida mit Minidump.
- **32-Bit-Wine-Spiele:** Der Gadget-Loader kann nur 64-Bit. Für 32-Bit braucht es das
  x86-Gadget und eine 32-Bit-Variante des Ladens (stdcall, 32-Bit-Register).
- **Gadget-Loader:** Fängt einen Thread per Software-Breakpoint an einer häufig
  aufgerufenen Windows-Funktion (Message-Pump, Timing). Spiele, die keine dieser
  Funktionen aufrufen, brauchen eigene `hot_points`.
- **Test-Suite:** Ein Durchlauf ist einmal mit einem Speicherabzug abgebrochen; weitere
  Durchläufe liefen sauber. Ursache nicht untersucht, möglicherweise flaky.
