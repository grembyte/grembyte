"""Shared settings for table widgets."""

# Leaves room for the inline editors (line edits, combo boxes) of editable cells.
TABLE_ROW_HEIGHT = 36


def set_row_height(table):
    """Give all rows of ``table`` the shared row height."""
    table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
