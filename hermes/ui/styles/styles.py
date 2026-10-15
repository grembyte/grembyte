"""Application-wide Qt stylesheet for hermes-hook (dark GitHub theme)."""

# NOTE: Qt style sheets do not resolve the generic family name "monospace",
# so concrete font names must be listed. Add your preferred font first.
MONO = "'JetBrains Mono', 'Fira Code', 'DejaVu Sans Mono', 'Liberation Mono'"

STYLE = """
    /* ------------------------------------------------------------------
       Base
       ------------------------------------------------------------------ */
    QMainWindow, QWidget {
        background-color: #0d1117;
        color: #c9d1d9;
        font-size: 13px;
    }
    /* Labels must never paint their own background (visible as dark
       patches on the lighter toolbar and status bar otherwise). */
    QLabel {
        background: transparent;
    }

    /* ------------------------------------------------------------------
       Panels: one subtle frame, title clearly above the field labels
       ------------------------------------------------------------------ */
    QGroupBox {
        border: 1px solid #21262d;
        border-radius: 8px;
        margin-top: 14px;
        padding: 14px 10px 10px 10px;
        color: #c9d1d9;
        font-size: 11px;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
    }
    /* Field labels inside panels: quieter and smaller than the title */
    QGroupBox QLabel {
        color: #8b949e;
        font-size: 12px;
        font-weight: normal;
        text-transform: none;
        letter-spacing: 0;
    }

    /* ------------------------------------------------------------------
       Tables: no own frame (the panel already has one), no focus rectangle
       ------------------------------------------------------------------ */
    QTableWidget {
        background-color: #0d1117;
        border: none;
        outline: none;
        gridline-color: #21262d;
        selection-background-color: rgba(31, 111, 235, 0.28);
        selection-color: #ffffff;
        alternate-background-color: #11161d;
        font-family: __MONO__;
        font-size: 12px;
    }
    QTableWidget::item {
        padding: 4px 8px;
        border: none;
    }
    QTableWidget::item:selected {
        background-color: rgba(31, 111, 235, 0.28);
        color: #ffffff;
    }
    QHeaderView {
        background-color: #161b22;
    }
    QHeaderView::section {
        background-color: #161b22;
        color: #8b949e;
        border: none;
        border-bottom: 1px solid #30363d;
        padding: 6px 8px;
        font-size: 11px;
        font-weight: normal;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    /* Selected rows would otherwise turn the header text bold */
    QHeaderView::section:checked {
        font-weight: normal;
        background-color: #161b22;
    }

    /* ------------------------------------------------------------------
       Buttons
       ------------------------------------------------------------------ */
    QPushButton {
        background-color: #21262d;
        color: #c9d1d9;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 6px 14px;
        font-size: 12px;
    }
    QPushButton:hover {
        background-color: #30363d;
        border-color: #58a6ff;
        color: #58a6ff;
    }
    QPushButton:pressed {
        background-color: #1f6feb;
        border-color: #1f6feb;
        color: #ffffff;
    }
    QPushButton:disabled {
        background-color: #161b22;
        border-color: #21262d;
        color: #484f58;
    }

    /* Primary action: use the object name or the dynamic property
       primary="true" so the highlighted button can follow the workflow. */
    QPushButton#btn_primary, QPushButton[primary="true"] {
        background-color: #1f6feb;
        border-color: #1f6feb;
        color: #ffffff;
    }
    QPushButton#btn_primary:hover, QPushButton[primary="true"]:hover {
        background-color: #388bfd;
        border-color: #388bfd;
        color: #ffffff;
    }
    QPushButton#btn_primary:disabled, QPushButton[primary="true"]:disabled {
        background-color: #12315f;
        border-color: #12315f;
        color: #6e7f99;
    }

    QPushButton#btn_danger {
        background-color: #21262d;
        border-color: #f85149;
        color: #f85149;
    }
    QPushButton#btn_danger:hover {
        background-color: #f85149;
        color: #ffffff;
    }
    QPushButton#btn_danger:disabled {
        background-color: #161b22;
        border-color: #4a2523;
        color: #6e3a37;
    }

    /* ------------------------------------------------------------------
       Inputs
       ------------------------------------------------------------------ */
    QLineEdit, QComboBox {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 6px 10px;
        color: #c9d1d9;
        font-family: __MONO__;
        font-size: 13px;
    }
    QLineEdit:focus, QComboBox:focus {
        border-color: #58a6ff;
    }
    QComboBox QAbstractItemView {
        background-color: #161b22;
        border: 1px solid #30363d;
        selection-background-color: #1f6feb;
        outline: none;
    }

    /* ------------------------------------------------------------------
       Layout chrome
       ------------------------------------------------------------------ */
    QSplitter::handle {
        background-color: transparent;
    }
    QSplitter::handle:horizontal {
        width: 6px;
    }
    QSplitter::handle:vertical {
        height: 6px;
    }
    QSplitter::handle:hover {
        background-color: #1f6feb;
    }

    QToolBar {
        background-color: #161b22;
        border-bottom: 1px solid #30363d;
        spacing: 6px;
        padding: 4px;
    }
    QToolBar::separator {
        background-color: #30363d;
        width: 1px;
        margin: 4px 8px;
    }

    QStatusBar {
        background-color: #161b22;
        border-top: 1px solid #30363d;
        color: #8b949e;
        font-size: 11px;
    }
    QStatusBar::item {
        border: none;
    }
    QStatusBar QLabel {
        color: #8b949e;
        font-size: 11px;
        padding: 0 10px;
    }

    QProgressBar {
        background-color: #161b22;
        border: none;
        border-radius: 3px;
        max-height: 6px;
        color: transparent;
    }
    QProgressBar::chunk {
        background-color: #1f6feb;
        border-radius: 3px;
    }

    /* ------------------------------------------------------------------
       Menus, tooltips, scroll bars
       ------------------------------------------------------------------ */
    QMenu {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 4px;
    }
    QMenu::item {
        padding: 6px 20px;
        border-radius: 4px;
    }
    QMenu::item:selected {
        background-color: #1f6feb;
        color: #ffffff;
    }
    QToolTip {
        background-color: #161b22;
        color: #c9d1d9;
        border: 1px solid #30363d;
        padding: 4px 6px;
    }

    QScrollBar:vertical {
        background: transparent;
        width: 10px;
        margin: 0;
    }
    QScrollBar:horizontal {
        background: transparent;
        height: 10px;
        margin: 0;
    }
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {
        background: #30363d;
        border-radius: 5px;
        min-height: 24px;
        min-width: 24px;
    }
    QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {
        background: #484f58;
    }
    QScrollBar::add-line, QScrollBar::sub-line,
    QScrollBar::add-page, QScrollBar::sub-page {
        background: transparent;
        border: none;
        width: 0;
        height: 0;
    }

    /* ------------------------------------------------------------------
       Lists (process dialog)
       ------------------------------------------------------------------ */
    QListWidget {
        background-color: #0d1117;
        border: 1px solid #30363d;
        border-radius: 4px;
        alternate-background-color: #11161d;
        outline: none;
    }
    QListWidget::item {
        padding: 3px 6px;
        color: #3fb950;
        font-family: __MONO__;
        font-size: 12px;
    }
    QListWidget::item:selected {
        background-color: rgba(31, 111, 235, 0.28);
        color: #ffffff;
    }
    QListWidget::item:hover {
        background-color: #21262d;
    }

    /* ------------------------------------------------------------------
       Named widgets
       ------------------------------------------------------------------ */
    QLabel#lbl_addr {
        color: #3fb950;
        font-family: __MONO__;
    }
    QLabel#lbl_title {
        color: #58a6ff;
        font-size: 15px;
        font-weight: bold;
    }
    QLabel#lbl_process {
        color: #f0883e;
        font-size: 12px;
    }
    QFrame#divider {
        background-color: #21262d;
        border: none;
        min-height: 1px;
        max-height: 1px;
    }
""".replace("__MONO__", MONO)
