"""Addresses of the project on the web that the application opens."""

# The online documentation; pages are given relative to it, e.g. "guide/scripts/".
DOCS_URL = "https://winterhafen.github.io/hermes-hook/"
# The bug report form of the issue tracker
BUG_REPORT_URL = "https://github.com/winterhafen/hermes-hook/issues/new?template=bug_report.yml"


def docs_url(page=""):
    """The address of ``page`` (e.g. "guide/scripts/#parameters") in the documentation."""
    return DOCS_URL + page
