# Hermes Hook

Hermes Hook ist eine Linux-Desktopanwendung auf Basis von Python und PyQt6. Das Projekt ist derzeit in Entwicklung: Die grafische Oberfläche und die Prozessauswahl starten, aber Speicher-Scans, Speicheränderungen, Freeze und Hooks sind noch nicht implementiert.

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

## Linux-Rechte für spätere Speicherzugriffe

Derzeit liest oder verändert die Anwendung keinen Prozessspeicher. Sobald diese Funktionen implementiert sind, können Linux-Schutzmechanismen wie Yama (`kernel.yama.ptrace_scope`) den Zugriff auf andere Prozesse einschränken. Dafür sind in der Regel passende Benutzerrechte und Zugriff auf den Zielprozess nötig. Starte die Anwendung nicht vorsorglich als root und lockere `ptrace_scope` nicht systemweit; passe Berechtigungen erst gezielt an den tatsächlichen Anwendungsfall an.
