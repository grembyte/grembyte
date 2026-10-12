"""
hermes-hook  –  RPG Test-Applikation
Dient als Ziel-Prozess zum Testen des Memory-Scanners.

Charakter-Attribute (5–18), Charakteransicht und
rundenbasierter Kampf gegen eine Riesenratte.
"""

import sys
import random


from PyQt6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QSpinBox, QGroupBox, QProgressBar, QPushButton,
    QTextEdit, QSplitter, QFrame, QGridLayout
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal  # pylint: disable=import-error,no-name-in-module
from PyQt6.QtGui import QTextCursor  # pylint: disable=import-error,no-name-in-module

# ──────────────────────────────────────────────────────────────────────────────
# Stilkonstanten  (parchment-meets-terminal – weder cream noch near-black)
# ──────────────────────────────────────────────────────────────────────────────
BG_DEEP   = "#1a1510"   # sehr dunkles Pergament-Braun
BG_MID    = "#241d14"   # Panel-Hintergrund
BG_LIGHT  = "#2f2518"   # Formular-Hintergrund
BORDER    = "#4a3828"   # Rahmenfarbe
GOLD      = "#c9962a"   # Akzent: Gold
AMBER     = "#e8a838"   # heller Akzent
RED       = "#c0392b"   # Schaden / HP
BLUE      = "#2980b9"   # Mana
GREEN     = "#27ae60"   # Heilung / positiv
GRAY      = "#7a6a58"   # inaktiver Text
WHITE     = "#e8dcc8"   # Haupttext (leicht gebrochen)
RAT_COL   = "#a05c2a"   # Riesenratte

FONT_DISPLAY = "UnifrakturMaguntia, Palatino Linotype, Georgia, serif"
FONT_BODY    = "JetBrains Mono, Fira Code, Consolas, monospace"

STYLE = f"""
QMainWindow, QWidget {{
    background-color: {BG_DEEP};
    color: {WHITE};
    font-family: {FONT_BODY};
    font-size: 13px;
}}
QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    margin-top: 14px;
    padding-top: 10px;
    color: {GOLD};
    font-size: 11px;
    letter-spacing: 1.5px;
    text-transform: uppercase;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
}}
QSpinBox {{
    background-color: {BG_LIGHT};
    border: 1px solid {BORDER};
    border-radius: 3px;
    color: {AMBER};
    padding: 3px 6px;
    font-size: 14px;
    font-weight: bold;
    min-width: 52px;
}}
QSpinBox::up-button, QSpinBox::down-button {{
    background-color: {BORDER};
    border: none;
    width: 18px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: {GOLD};
}}
QPushButton {{
    background-color: {BG_LIGHT};
    color: {WHITE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 7px 18px;
    font-family: {FONT_BODY};
    font-size: 12px;
    letter-spacing: 0.5px;
}}
QPushButton:hover {{
    background-color: {BORDER};
    border-color: {GOLD};
    color: {AMBER};
}}
QPushButton:pressed {{
    background-color: {GOLD};
    color: {BG_DEEP};
}}
QPushButton:disabled {{
    background-color: {BG_MID};
    color: {GRAY};
    border-color: {BG_LIGHT};
}}
QPushButton#btn_attack {{
    border-color: {AMBER};
    color: {AMBER};
}}
QPushButton#btn_spell {{
    border-color: {BLUE};
    color: #5dade2;
}}
QPushButton#btn_rest {{
    border-color: {GREEN};
    color: #58d68d;
}}
QPushButton#btn_new {{
    border-color: {RED};
    color: #e74c3c;
}}
QProgressBar {{
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {BG_LIGHT};
    text-align: center;
    color: {WHITE};
    font-size: 11px;
    height: 16px;
}}
QProgressBar::chunk#hp_bar {{
    background-color: {RED};
    border-radius: 2px;
}}
QProgressBar::chunk#mana_bar {{
    background-color: {BLUE};
    border-radius: 2px;
}}
QProgressBar::chunk#xp_bar {{
    background-color: {GOLD};
    border-radius: 2px;
}}
QTextEdit {{
    background-color: {BG_MID};
    border: 1px solid {BORDER};
    border-radius: 4px;
    color: {WHITE};
    font-family: {FONT_BODY};
    font-size: 12px;
    padding: 6px;
}}
QSplitter::handle {{
    background-color: {BORDER};
}}
QLabel#display_name {{
    color: {GOLD};
    font-size: 20px;
    font-weight: bold;
    letter-spacing: 2px;
}}
QLabel#display_level {{
    color: {AMBER};
    font-size: 12px;
}}
QLabel#stat_val {{
    color: {AMBER};
    font-size: 16px;
    font-weight: bold;
    min-width: 32px;
}}
QLabel#derived_val {{
    color: {WHITE};
    font-size: 13px;
}}
QLabel#section_title {{
    color: {GOLD};
    font-size: 13px;
    letter-spacing: 1px;
    border-bottom: 1px solid {BORDER};
    padding-bottom: 2px;
}}
QLabel#combat_name {{
    font-size: 15px;
    font-weight: bold;
}}
QFrame#divider {{
    background-color: {BORDER};
    max-height: 1px;
}}
"""

# ──────────────────────────────────────────────────────────────────────────────
# Spiellogik
# ──────────────────────────────────────────────────────────────────────────────

ATTR_DEFS = [
    ("staerke",       "Stärke",      "STR", "⚔"),
    ("intelligenz",   "Intelligenz", "INT", "✦"),
    ("wachsamkeit",   "Wachsamkeit", "WCH", "👁"),
    ("geschicklichkeit","Geschicklichkeit","GES","◈"),
    ("ausdauer",      "Ausdauer",    "AUS", "♥"),
    ("charisma",      "Charisma",    "CHA", "❋"),
    ("gesundheit",    "Gesundheit",  "GES_MAX", "❤"),
    ("mana",          "Mana",        "MANA_MAX","✦"),
]


class Character:
    """Spieler-Charakter mit abgeleiteten Werten."""

    # A character stores both primary and derived game state.
    # pylint: disable=too-many-instance-attributes

    def __init__(self):
        # Primärattribute (5–18)
        self.staerke           = 10
        self.intelligenz       = 10
        self.wachsamkeit       = 10
        self.geschicklichkeit  = 10
        self.ausdauer          = 10
        self.charisma          = 10
        self.gesundheit        = 14   # Einfluss auf max_hp
        self.mana              = 12   # Einfluss auf max_mana

        self.level = 1
        self.xp    = 0
        self.xp_next = 100

        self.recalculate()

    def recalculate(self):
        """Update derived health and mana limits from the primary attributes."""
        self.max_hp   = 20 + self.gesundheit * 3 + self.ausdauer * 2
        self.max_mana = 10 + self.mana * 3 + self.intelligenz * 2
        # Kap auf aktuelle Werte
        self.hp   = min(getattr(self, "hp",   self.max_hp),   self.max_hp)
        self.mana_cur = min(getattr(self, "mana_cur", self.max_mana), self.max_mana)

    # Abgeleitete Kampfwerte
    @property
    def angriff(self):
        """Return the character's weapon damage bonus."""
        return self.staerke // 3 + 2               # Waffe: 1d6 + angriff

    @property
    def ruestung(self):
        """Return the character's damage reduction."""
        return max(0, self.wachsamkeit // 4)       # Schadensreduzierung

    @property
    def zauberschaden(self):
        """Return the character's spell damage bonus."""
        return self.intelligenz // 3 + 3           # Feuerball: 1d8 + zauberschaden

    @property
    def ausweichen(self):
        """Return the character's dodge threshold."""
        return self.geschicklichkeit // 4          # Ausweichchance in %*5

    def gain_xp(self, amount: int) -> bool:
        """Gibt True zurück wenn Level-Up."""
        self.xp += amount
        if self.xp >= self.xp_next:
            self.xp -= self.xp_next
            self.level += 1
            self.xp_next = int(self.xp_next * 1.5)
            self.hp = self.max_hp   # HP bei Level-Up füllen
            return True
        return False


class GiantRat:
    """Gegner: Riesenratte."""

    BASE_HP     = 18
    BASE_ATK    = 5    # Basis-Schaden
    BASE_ARMOR  = 1

    def __init__(self, level: int = 1):
        self.level   = level
        self.max_hp  = self.BASE_HP + level * 4
        self.hp      = self.max_hp
        self.atk     = self.BASE_ATK + level
        self.armor   = self.BASE_ARMOR
        self.xp_reward = 20 + level * 10

    @property
    def name(self):
        """Return the rat's level-appropriate display name."""
        prefixes = {
            1: "Gewöhnliche",
            2: "Große",
            3: "Riesige",
            4: "Uralte",
        }
        prefix = prefixes.get(self.level, "Legendäre")
        return f"{prefix} Riesenratte"

    def attack(self, player: Character) -> tuple[int, str]:
        """Greift an, gibt (effSchaden, nachricht) zurück."""
        base = random.randint(1, self.atk)
        # Wachsamkeit reduziert Schaden
        reduction = player.ruestung
        # Ausweichen
        dodge_roll = random.randint(1, 20)
        if dodge_roll <= player.ausweichen:
            return 0, "ausgewichen"
        dmg = max(1, base - reduction)
        player.hp = max(0, player.hp - dmg)
        return dmg, f"trifft für {base} – {reduction} (Rüstung) = **{dmg}** Schaden"


# ──────────────────────────────────────────────────────────────────────────────
# Hilfs-Widget: Attribut-Zeile
# ──────────────────────────────────────────────────────────────────────────────

class AttrRow(QWidget):
    """Widget containing one primary attribute and its modifier indicator."""

    changed = pyqtSignal()

    def __init__(self, icon: str, label: str, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)

        ico = QLabel(icon)
        ico.setFixedWidth(20)
        ico.setStyleSheet(f"color: {GOLD}; font-size: 14px;")
        lay.addWidget(ico)

        lbl = QLabel(label)
        lbl.setFixedWidth(140)
        lbl.setStyleSheet(f"color: {WHITE};")
        lay.addWidget(lbl)

        self.spin = QSpinBox()
        self.spin.setRange(5, 18)
        self.spin.setValue(10)
        self.spin.valueChanged.connect(self.changed.emit)
        lay.addWidget(self.spin)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(5, 18)
        self.progress_bar.setValue(10)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid {BORDER};
                background: {BG_LIGHT};
                border-radius: 2px;
            }}
            QProgressBar::chunk {{ background: {GOLD}; border-radius:2px; }}
        """)
        self.spin.valueChanged.connect(self.progress_bar.setValue)
        lay.addWidget(self.progress_bar, 1)

        self.mod_lbl = QLabel("+0")
        self.mod_lbl.setFixedWidth(30)
        self.mod_lbl.setStyleSheet(f"color: {GRAY}; font-size:11px;")
        lay.addWidget(self.mod_lbl)
        self.spin.valueChanged.connect(self._update_mod)

    def _update_mod(self, v):
        mod = (v - 10) // 2
        self.mod_lbl.setText(f"{mod:+d}")
        color = "#58d68d" if mod > 0 else RED if mod < 0 else GRAY
        self.mod_lbl.setStyleSheet(
            f"color: {color}; font-size:11px;"
        )

    @property
    def value(self):
        """Return the selected attribute value."""
        return self.spin.value()

    def set_value(self, v):
        """Set the selected attribute value."""
        self.spin.setValue(v)


# ──────────────────────────────────────────────────────────────────────────────
# Haupt-Fenster
# ──────────────────────────────────────────────────────────────────────────────

class GameWindow(QMainWindow):
    """Interactive RPG demo window used as a memory-scanner target."""

    # UI state is initialized by the dedicated Qt widget-builder methods.
    # pylint: disable=attribute-defined-outside-init,too-few-public-methods,too-many-instance-attributes

    def __init__(self):
        super().__init__()
        self.setWindowTitle("hermes-hook  //  RPG Testumgebung")
        self.setMinimumSize(1100, 780)
        self.resize(1280, 860)
        self.setStyleSheet(STYLE)

        self.char = Character()
        self.rat  = None
        self.combat_active = False
        self.player_turn   = True

        self._build_ui()
        self._sync_from_char()

    # ──────────────────────────────────────────────
    # UI aufbauen
    # ──────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        # Haupttrenner: oben Charakter, unten Kampf
        main_split = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(main_split)

        # --- Obere Hälfte: Attribute links, Anzeige rechts ---
        top = QWidget()
        top_lay = QHBoxLayout(top)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(8)
        top_lay.addWidget(self._build_attr_editor(), 1)
        top_lay.addWidget(self._build_char_display(), 1)
        main_split.addWidget(top)

        # --- Untere Hälfte: Kampf ---
        main_split.addWidget(self._build_combat_panel())
        main_split.setSizes([400, 360])

    # ── Attribut-Editor (links) ──────────────────
    def _build_attr_editor(self):
        box = QGroupBox("Charakter-Attribute")
        lay = QVBoxLayout(box)
        lay.setSpacing(4)

        header = QHBoxLayout()
        lbl_name = QLabel("§ Attribut")
        lbl_name.setStyleSheet(f"color:{GRAY}; font-size:11px;")
        header.addWidget(lbl_name)
        header.addStretch()
        lbl_range = QLabel("5 – 18   Mod")
        lbl_range.setStyleSheet(f"color:{GRAY}; font-size:11px;")
        header.addWidget(lbl_range)
        lay.addLayout(header)

        div = QFrame()
        div.setObjectName("divider")
        div.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(div)

        self.attr_rows = {}
        for key, label, _, icon in ATTR_DEFS:
            row = AttrRow(icon, label)
            row.changed.connect(self._on_attr_changed)
            lay.addWidget(row)
            self.attr_rows[key] = row

        lay.addStretch()

        # Zufallswürfeln
        btn_roll = QPushButton("⚄  Zufällig würfeln  (4W6 drop lowest)")
        btn_roll.clicked.connect(self._roll_stats)
        lay.addWidget(btn_roll)

        return box

    # ── Charakter-Anzeige (rechts) ───────────────
    def _build_char_display(self):  # pylint: disable=too-many-statements
        box = QGroupBox("Charakterblatt")
        lay = QVBoxLayout(box)
        lay.setSpacing(8)

        # Name & Level
        name_row = QHBoxLayout()
        self.lbl_name = QLabel("Abenteurer")
        self.lbl_name.setObjectName("display_name")
        name_row.addWidget(self.lbl_name)
        name_row.addStretch()
        self.lbl_level = QLabel("Level 1")
        self.lbl_level.setObjectName("display_level")
        name_row.addWidget(self.lbl_level)
        lay.addLayout(name_row)

        div = QFrame()
        div.setObjectName("divider")
        div.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(div)

        # HP & Mana Balken
        bars = QGridLayout()
        bars.setSpacing(6)

        bars.addWidget(QLabel("❤  Trefferpunkte"), 0, 0)
        self.hp_bar = QProgressBar()
        self.hp_bar.setObjectName("hp_bar")
        self.hp_bar.setFormat("%v / %m")
        bars.addWidget(self.hp_bar, 0, 1)
        self.lbl_hp = QLabel()
        self.lbl_hp.setObjectName("stat_val")
        bars.addWidget(self.lbl_hp, 0, 2)

        bars.addWidget(QLabel("✦  Mana"), 1, 0)
        self.mana_bar = QProgressBar()
        self.mana_bar.setObjectName("mana_bar")
        self.mana_bar.setFormat("%v / %m")
        bars.addWidget(self.mana_bar, 1, 1)
        self.lbl_mana = QLabel()
        self.lbl_mana.setObjectName("stat_val")
        self.lbl_mana.setStyleSheet("color: #5dade2; font-size:16px; font-weight:bold;")
        bars.addWidget(self.lbl_mana, 1, 2)

        bars.addWidget(QLabel("✧  Erfahrung"), 2, 0)
        self.xp_bar = QProgressBar()
        self.xp_bar.setObjectName("xp_bar")
        self.xp_bar.setFormat("XP: %v / %m")
        bars.addWidget(self.xp_bar, 2, 1, 1, 2)

        lay.addLayout(bars)

        div2 = QFrame()
        div2.setObjectName("divider")
        div2.setFrameShape(QFrame.Shape.HLine)
        lay.addWidget(div2)

        # Abgeleitete Kampfwerte
        lbl_sec = QLabel("Abgeleitete Kampfwerte")
        lbl_sec.setObjectName("section_title")
        lay.addWidget(lbl_sec)

        self.derived_grid = QGridLayout()
        self.derived_grid.setSpacing(4)
        self._derived_labels = {}
        derived_defs = [
            ("angriff",       "⚔  Waffenschaden-Bonus"),
            ("ruestung",      "🛡  Rüstungsreduktion"),
            ("zauberschaden", "✦  Zauberschaden-Bonus"),
            ("ausweichen",    "◈  Ausweichen ×5 %"),
            ("max_hp",        "❤  Max. Trefferpunkte"),
            ("max_mana",      "✦  Max. Mana"),
        ]
        for i, (key, label) in enumerate(derived_defs):
            lbl_k = QLabel(label)
            lbl_k.setStyleSheet(f"color:{GRAY};")
            lbl_v = QLabel("–")
            lbl_v.setObjectName("derived_val")
            self.derived_grid.addWidget(lbl_k, i, 0)
            self.derived_grid.addWidget(lbl_v, i, 1)
            self._derived_labels[key] = lbl_v
        lay.addLayout(self.derived_grid)

        lay.addStretch()
        return box

    # ── Kampf-Panel (unten) ──────────────────────
    def _build_combat_panel(self):  # pylint: disable=too-many-statements
        box = QGroupBox("Rundenbasierter Kampf")
        lay  = QVBoxLayout(box)
        lay.setSpacing(8)

        # Zustandsanzeige beider Kämpfer
        combatants = QHBoxLayout()

        # Spieler
        player_box = QGroupBox("Spieler")
        pb_lay = QVBoxLayout(player_box)
        self.lbl_player_name = QLabel("Abenteurer")
        self.lbl_player_name.setObjectName("combat_name")
        self.lbl_player_name.setStyleSheet(f"color:{AMBER};")
        pb_lay.addWidget(self.lbl_player_name)
        self.combat_hp_bar = QProgressBar()
        self.combat_hp_bar.setObjectName("hp_bar")
        self.combat_hp_bar.setFormat("HP: %v / %m")
        pb_lay.addWidget(self.combat_hp_bar)
        self.combat_mana_bar = QProgressBar()
        self.combat_mana_bar.setObjectName("mana_bar")
        self.combat_mana_bar.setFormat("Mana: %v / %m")
        pb_lay.addWidget(self.combat_mana_bar)
        combatants.addWidget(player_box)

        # VS
        vs_lbl = QLabel("⚔")
        vs_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        vs_lbl.setStyleSheet(f"color:{GOLD}; font-size:24px;")
        combatants.addWidget(vs_lbl)

        # Ratte
        rat_box = QGroupBox("Gegner")
        rb_lay = QVBoxLayout(rat_box)
        self.lbl_rat_name = QLabel("–")
        self.lbl_rat_name.setObjectName("combat_name")
        self.lbl_rat_name.setStyleSheet(f"color:{RAT_COL};")
        rb_lay.addWidget(self.lbl_rat_name)
        self.rat_hp_bar = QProgressBar()
        self.rat_hp_bar.setObjectName("hp_bar")
        self.rat_hp_bar.setFormat("HP: %v / %m")
        self.rat_hp_bar.setStyleSheet(f"""
            QProgressBar {{ border:1px solid {BORDER}; background:{BG_LIGHT}; border-radius:3px; }}
            QProgressBar::chunk {{ background: {RAT_COL}; border-radius:2px; }}
        """)
        rb_lay.addWidget(self.rat_hp_bar)
        combatants.addWidget(rat_box)

        lay.addLayout(combatants)

        # Aktions-Buttons
        btn_row = QHBoxLayout()
        self.btn_new_fight = QPushButton("🐀  Neuer Kampf")
        self.btn_new_fight.setObjectName("btn_new")
        self.btn_new_fight.clicked.connect(self._new_fight)

        self.btn_attack = QPushButton("⚔  Angriff (Waffe)")
        self.btn_attack.setObjectName("btn_attack")
        self.btn_attack.setEnabled(False)
        self.btn_attack.clicked.connect(self._player_attack)

        self.btn_spell = QPushButton("✦  Feuerball (8 Mana)")
        self.btn_spell.setObjectName("btn_spell")
        self.btn_spell.setEnabled(False)
        self.btn_spell.clicked.connect(self._player_spell)

        self.btn_rest = QPushButton("☽  Rasten (+HP, +Mana)")
        self.btn_rest.setObjectName("btn_rest")
        self.btn_rest.setEnabled(False)
        self.btn_rest.clicked.connect(self._player_rest)

        btn_row.addWidget(self.btn_new_fight)
        btn_row.addWidget(self.btn_attack)
        btn_row.addWidget(self.btn_spell)
        btn_row.addWidget(self.btn_rest)
        lay.addLayout(btn_row)

        # Kampflog
        self.combat_log = QTextEdit()
        self.combat_log.setReadOnly(True)
        self.combat_log.setMinimumHeight(140)
        lay.addWidget(self.combat_log)

        return box

    # ──────────────────────────────────────────────
    # Synchronisation
    # ──────────────────────────────────────────────

    def _read_attrs_into_char(self):
        for key, _, _, _ in ATTR_DEFS:
            setattr(self.char, key, self.attr_rows[key].value)
        self.char.recalculate()

    def _sync_from_char(self):
        c = self.char
        self.lbl_level.setText(f"Level {c.level}")
        self.lbl_name.setText("Abenteurer")
        self.lbl_player_name.setText("Abenteurer")

        self.hp_bar.setMaximum(c.max_hp)
        self.hp_bar.setValue(c.hp)
        self.lbl_hp.setText(f"{c.hp}")

        self.mana_bar.setMaximum(c.max_mana)
        self.mana_bar.setValue(c.mana_cur)
        self.lbl_mana.setText(f"{c.mana_cur}")

        self.xp_bar.setMaximum(c.xp_next)
        self.xp_bar.setValue(c.xp)

        self._derived_labels["angriff"].setText(f"+{c.angriff}  (1W6+{c.angriff})")
        self._derived_labels["ruestung"].setText(f"{c.ruestung} Punkte")
        self._derived_labels["zauberschaden"].setText(
            f"+{c.zauberschaden}  (1W8+{c.zauberschaden})"
        )
        self._derived_labels["ausweichen"].setText(f"{c.ausweichen * 5} %")
        self._derived_labels["max_hp"].setText(str(c.max_hp))
        self._derived_labels["max_mana"].setText(str(c.max_mana))

        # Kampf-Balken
        self.combat_hp_bar.setMaximum(c.max_hp)
        self.combat_hp_bar.setValue(c.hp)
        self.combat_mana_bar.setMaximum(c.max_mana)
        self.combat_mana_bar.setValue(c.mana_cur)

        if self.rat:
            self._sync_rat()

    def _sync_rat(self):
        self.rat_hp_bar.setMaximum(self.rat.max_hp)
        self.rat_hp_bar.setValue(self.rat.hp)
        self.lbl_rat_name.setText(self.rat.name)

    # ──────────────────────────────────────────────
    # Events
    # ──────────────────────────────────────────────

    def _on_attr_changed(self):
        self._read_attrs_into_char()
        self._sync_from_char()

    def _roll_stats(self):
        def roll4d6():
            rolls = [random.randint(1, 6) for _ in range(4)]
            return sum(sorted(rolls)[1:])   # drop lowest
        for key, _, _, _ in ATTR_DEFS:
            val = max(5, min(18, roll4d6()))
            self.attr_rows[key].set_value(val)

    # ──────────────────────────────────────────────
    # Kampflogik
    # ──────────────────────────────────────────────

    def _log(self, msg: str, color: str = WHITE):
        self.combat_log.append(
            f'<span style="color:{color}; font-family:{FONT_BODY};">{msg}</span>'
        )
        self.combat_log.moveCursor(QTextCursor.MoveOperation.End)

    def _new_fight(self):
        self._read_attrs_into_char()
        self.char.hp      = self.char.max_hp
        self.char.mana_cur = self.char.max_mana
        rat_level = max(1, min(4, self.char.level))
        self.rat  = GiantRat(rat_level)
        self.combat_active = True
        self.player_turn   = True

        self._sync_from_char()
        self.combat_log.clear()
        self._log("━━━  Neuer Kampf  ━━━", GOLD)
        self._log(
            f"Eine {self.rat.name} erscheint! "
            f"(HP: {self.rat.max_hp}, Angriff: {self.rat.atk})",
            RAT_COL,
        )
        self._log("Dein Zug, Abenteurer!", AMBER)
        self._set_combat_buttons(True)

    def _set_combat_buttons(self, enabled: bool):
        self.btn_attack.setEnabled(enabled)
        self.btn_spell.setEnabled(enabled and self.char.mana_cur >= 8)
        self.btn_rest.setEnabled(enabled)

    def _check_combat_end(self) -> bool:
        if self.rat.hp <= 0:
            self._log(f"Die {self.rat.name} wurde besiegt!", GREEN)
            xp = self.rat.xp_reward
            level_up = self.char.gain_xp(xp)
            self._log(f"+{xp} Erfahrungspunkte.", GOLD)
            if level_up:
                self._log(f"✦ LEVEL UP! Du bist nun Level {self.char.level}! ✦", AMBER)
            self.combat_active = False
            self._set_combat_buttons(False)
            self._sync_from_char()
            return True
        if self.char.hp <= 0:
            self._log("Du wurdest besiegt … 💀", RED)
            self.combat_active = False
            self._set_combat_buttons(False)
            self._sync_from_char()
            return True
        return False

    def _rat_turn(self):
        if not self.combat_active:
            return
        self._log(f"  [{self.rat.name} greift an]", RAT_COL)
        dmg, msg = self.rat.attack(self.char)
        if dmg == 0:
            self._log("  Du weichst dem Angriff aus!", GREEN)
        else:
            self._log(f"  {msg}", RED)
        self._sync_from_char()
        if not self._check_combat_end():
            self._log("Dein Zug.", AMBER)
            self.player_turn = True
            self._set_combat_buttons(True)

    def _player_attack(self):
        if not self.combat_active or not self.player_turn:
            return
        self._set_combat_buttons(False)
        self.player_turn = False

        roll   = random.randint(1, 6)
        bonus  = self.char.angriff
        total  = roll + bonus
        armor  = self.rat.armor
        dmg    = max(1, total - armor)
        self.rat.hp = max(0, self.rat.hp - dmg)

        self._log(
            f"Du schlägst mit der Waffe: W6({roll}) + {bonus} – {armor} "
            f"(Rüstung) = {dmg} Schaden",
            AMBER,
        )
        self._sync_rat()

        if not self._check_combat_end():
            QTimer.singleShot(600, self._rat_turn)

    def _player_spell(self):
        if not self.combat_active or not self.player_turn:
            return
        if self.char.mana_cur < 8:
            self._log("Nicht genug Mana für einen Feuerball!", RED)
            return
        self._set_combat_buttons(False)
        self.player_turn = False

        self.char.mana_cur -= 8
        roll   = random.randint(1, 8)
        bonus  = self.char.zauberschaden
        dmg    = roll + bonus
        self.rat.hp = max(0, self.rat.hp - dmg)

        self._log(f"Du wirkst Feuerball! W8({roll}) + {bonus} = {dmg} Schaden.  "
                  f"(-8 Mana, verbleibend: {self.char.mana_cur})", "#5dade2")
        self._sync_from_char()
        self._sync_rat()

        if not self._check_combat_end():
            QTimer.singleShot(600, self._rat_turn)

    def _player_rest(self):
        if not self.combat_active or not self.player_turn:
            return
        self._set_combat_buttons(False)
        self.player_turn = False

        hp_gain   = random.randint(2, 5) + max(0, (self.char.ausdauer - 10) // 2)
        mana_gain = random.randint(1, 4) + max(0, (self.char.intelligenz - 10) // 3)
        self.char.hp      = min(self.char.max_hp,   self.char.hp + hp_gain)
        self.char.mana_cur = min(self.char.max_mana, self.char.mana_cur + mana_gain)

        self._log(f"Du rastest kurz … +{hp_gain} HP, +{mana_gain} Mana.", GREEN)
        self._sync_from_char()

        if not self._check_combat_end():
            QTimer.singleShot(600, self._rat_turn)


# ──────────────────────────────────────────────────────────────────────────────
# Start
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = GameWindow()
    win.show()
    sys.exit(app.exec())
