"""About dialog with third-party component acknowledgements."""

from html import escape
from importlib.metadata import PackageNotFoundError, version

from PySide6.QtWidgets import (  # pylint: disable=import-error,no-name-in-module
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from grembyte import __version__
from grembyte.ui.widgets.icons import set_button_icon

_COMPONENTS = (
    ("Frida", "frida", "wxWindows Library Licence 3.1", "https://frida.re/",
     "https://www.wxwidgets.org/about/licence/"),
    ("Capstone", "capstone", "BSD 3-Clause", "https://www.capstone-engine.org/",
     "https://github.com/capstone-engine/capstone/blob/next/LICENSES/LICENSE.TXT"),
    ("psutil", "psutil", "BSD 3-Clause", "https://github.com/giampaolo/psutil",
     "https://github.com/giampaolo/psutil/blob/master/LICENSE"),
    ("NumPy", "numpy", "BSD 3-Clause", "https://numpy.org/",
     "https://github.com/numpy/numpy/blob/main/LICENSE.txt"),
    ("PySide6", "PySide6_Essentials", "LGPL v3", "https://doc.qt.io/qtforpython-6/",
     "https://www.qt.io/licensing/"),
    ("Qt 6", "PySide6_Essentials", "LGPL v3", "https://www.qt.io/",
     "https://www.qt.io/licensing/open-source-lgpl-obligations"),
    ("SQLAlchemy", "SQLAlchemy", "MIT", "https://www.sqlalchemy.org/",
     "https://github.com/sqlalchemy/sqlalchemy/blob/main/LICENSE"),
)
_ICON_COMPONENT = (
    "Google Material Icons (Outlined)", "", "Apache 2.0",
    "https://fonts.google.com/icons", "https://www.apache.org/licenses/LICENSE-2.0",
)


def _installed_version(distribution):
    if not distribution:
        return None
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


class AboutDialog(QDialog):
    """Show Grembyte's version and open-source component credits."""

    def __init__(self, translations, parent=None):
        super().__init__(parent)
        self.setWindowTitle(translations["about.title"])
        self.resize(760, 500)

        layout = QVBoxLayout(self)
        heading = QLabel(translations["about.project_version"].format(version=__version__))
        heading.setStyleSheet("font-weight: bold; font-size: 16px;")
        layout.addWidget(heading)
        notice = QLabel(translations["about.license_notice"])
        notice.setWordWrap(True)
        layout.addWidget(notice)

        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setReadOnly(True)
        browser.setHtml(self._credits_html(translations, browser))
        browser.setStyleSheet("border: none; background: transparent;")
        layout.addWidget(browser, 1)
        self.credits = browser

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_button = QPushButton(translations["about.close"])
        set_button_icon(close_button, "close")
        close_button.clicked.connect(self.accept)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    @staticmethod
    def _credits_html(translations, browser):
        palette = browser.palette()
        text_color = palette.color(palette.ColorRole.Text).name()
        link_color = palette.color(palette.ColorRole.Link).name()
        border_color = palette.color(palette.ColorRole.Mid).name()
        components = [
            (*component[:2], _installed_version(component[1]), *component[2:])
            for component in _COMPONENTS
        ]
        components.append((*_ICON_COMPONENT[:2], translations["about.bundled"],
                           *_ICON_COMPONENT[2:]))
        rows = []
        for name, _distribution, installed, license_name, project_url, license_url in components:
            display_name = escape(name)
            project_link = (f'<a href="{escape(project_url, quote=True)}">{display_name}</a>')
            license_link = (
                f'<a href="{escape(license_url, quote=True)}">{escape(license_name)}</a>')
            rows.append(
                f"<tr><td>{project_link}</td><td>{escape(installed or translations['about.unknown'])}"
                f"</td><td>{license_link}</td></tr>")

        return (
            f'<p style="color:{text_color}">{escape(translations["about.intro"])}</p>'
            '<table width="100%" cellspacing="0" cellpadding="6">'
            '<thead><tr>'
            f'<th align="left">{escape(translations["about.component"])}</th>'
            f'<th align="left">{escape(translations["about.version"])}</th>'
            f'<th align="left">{escape(translations["about.license"])}</th>'
            '</tr></thead><tbody>'
            f'{"".join(rows)}'
            '</tbody></table>'
            f'<p style="color:{link_color}">{escape(translations["about.icon_note"])}</p>'
            f'<style>td, th {{ border-bottom: 1px solid {border_color}; }} '
            f'a {{ color: {link_color}; }}</style>'
        )