"""Application-wide Qt stylesheet for hermes-hook (dark GitHub theme)."""

STYLE = """
    QMainWindow, QWidget {
        background-color: #0d1117;
        color: #c9d1d9;
        font-family: 'JetBrains Mono', 'Fira Code', monospace;
        font-size: 13px;
    }

    QGroupBox {
        border: 1px solid #30363d;
        border-radius: 6px;
        margin-top: 12px;
        padding-top: 8px;
        color: #8b949e;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
    }

    QTableWidget {
        background-color: #0d1117;
        border: 1px solid #30363d;
        border-radius: 4px;
        gridline-color: #21262d;
        selection-background-color: #1f6feb;
        selection-color: #ffffff;
        alternate-background-color: #161b22;
    }
    QTableWidget::item {
        padding: 4px 8px;
        border: none;
    }
    QHeaderView::section {
        background-color: #161b22;
        color: #8b949e;
        border: none;
        border-bottom: 1px solid #30363d;
        border-right: 1px solid #21262d;
        padding: 6px 8px;
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    QPushButton {
        background-color: #21262d;
        color: #c9d1d9;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 6px 14px;
        font-family: 'JetBrains Mono', monospace;
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
    QPushButton#btn_primary {
        background-color: #1f6feb;
        border-color: #1f6feb;
        color: #ffffff;
    }
    QPushButton#btn_primary:hover {
        background-color: #388bfd;
        border-color: #388bfd;
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

    QLineEdit, QComboBox {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 6px;
        padding: 6px 10px;
        color: #c9d1d9;
        font-family: 'JetBrains Mono', monospace;
        font-size: 13px;
    }
    QLineEdit:focus, QComboBox:focus {
        border-color: #58a6ff;
    }
    QComboBox::drop-down {
        border: none;
        padding-right: 8px;
    }
    QComboBox QAbstractItemView {
        background-color: #161b22;
        border: 1px solid #30363d;
        selection-background-color: #1f6feb;
    }

    QSplitter::handle {
        background-color: #21262d;
        width: 2px;
        height: 2px;
    }
    QSplitter::handle:hover {
        background-color: #58a6ff;
    }

    QStatusBar {
        background-color: #161b22;
        border-top: 1px solid #30363d;
        color: #8b949e;
        font-size: 11px;
    }

    QToolBar {
        background-color: #161b22;
        border-bottom: 1px solid #30363d;
        spacing: 4px;
        padding: 4px;
    }

    QListWidget {
        background-color: #0d1117;
        border: 1px solid #30363d;
        border-radius: 4px;
        alternate-background-color: #161b22;
    }
    QListWidget::item {
        padding: 3px 6px;
        color: #3fb950;
        font-family: 'JetBrains Mono', monospace;
        font-size: 12px;
    }
    QListWidget::item:selected {
        background-color: #1f6feb;
        color: #ffffff;
    }
    QListWidget::item:hover {
        background-color: #21262d;
    }

    QLabel#lbl_addr {
        color: #3fb950;
        font-family: 'JetBrains Mono', monospace;
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
        background-color: #30363d;
        max-height: 1px;
    }
"""
