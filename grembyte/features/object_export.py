"""An object of the object inspector as a self-contained HTML page.

The page shows what the inspector showed when it was saved: the object's
class, name and address, its fields, the values of its getters and its
instance methods (those of its base classes too), plus the program it was
read from. It needs nothing besides itself (no scripts, no
files), follows the browser's light or dark mode, and escapes every text of
the game.
"""

import html
import re

_UNSAFE = re.compile(r"[^\w.-]+")

# Per table: (column title key, width). Addresses and offsets get the width
# of their digits; name and type a share; the value (parameters) the rest.
_COLUMNS = {
    "fields": (("inspector.column_field", "26%"), ("inspector.column_type", "16%"),
               ("inspector.column_value", None), ("inspector.column_offset", "8ch"),
               ("inspector.export_address", "17ch")),
    "properties": (("inspector.column_property", "26%"), ("inspector.column_type", "20%"),
                   ("inspector.column_value", None)),
    "methods": (("inspector.column_method", "22%"), ("inspector.column_params", None),
                ("inspector.column_returns", "18%"), ("inspector.column_owner", "20%")),
}

_STYLE = """
:root { color-scheme: light dark; --muted: #6b7280; --line: #d1d5db; --head: #f3f4f6;
        --accent: #2563eb; --danger: #dc2626; }
@media (prefers-color-scheme: dark) {
  :root { --muted: #9ca3af; --line: #374151; --head: #1f2937; --accent: #60a5fa;
          --danger: #f87171; }
}
body { font-family: system-ui, sans-serif; margin: 2rem; line-height: 1.4; }
h1 { font-size: 1.4rem; margin-bottom: .25rem; word-break: break-all; }
h2 { font-size: 1.1rem; margin-top: 2rem; }
dl { display: grid; grid-template-columns: max-content auto; gap: .2rem 1rem; color: var(--muted); }
dd { margin: 0; word-break: break-all; }
/* Fixed column shares (see _COLUMNS): a long type name must not squeeze the value. */
table { border-collapse: collapse; width: 100%; font-size: .9rem; table-layout: fixed; }
th, td { border: 1px solid var(--line); padding: .3rem .5rem; text-align: left;
         vertical-align: top; overflow-wrap: anywhere; }
th { background: var(--head); }
td.value { font-family: ui-monospace, monospace; white-space: pre-wrap; }
td.offset, td.address { font-family: ui-monospace, monospace; white-space: nowrap;
                        overflow-wrap: normal; }
td.type, td.owner { color: var(--muted); }
tr.static td { color: var(--muted); }
td.error, p.error { color: var(--danger); }
footer { margin-top: 2rem; color: var(--muted); font-size: .8rem; }
"""


def file_name(snapshot):
    """A file name for ``snapshot``: its name (or class) and address, and when it was saved."""
    name = snapshot.get("instance_name") or snapshot.get("short_class") or "object"
    stamp = snapshot["time"].strftime("%Y%m%d-%H%M%S")
    parts = [_UNSAFE.sub("_", part).strip("_") for part in (name, snapshot.get("address", ""))]
    return "-".join(part for part in (*parts, stamp) if part) + ".html"


def render_html(snapshot, translations, language="en"):
    """The page for ``snapshot``.

    ``snapshot`` holds ``class_name``, ``instance_name``, ``address``,
    ``length``, ``captured`` (when the values were recorded at a call),
    ``time`` (a datetime), ``version``, ``program`` ({name, path, pid} or
    None), ``fields`` ([{name, type, value, offset, address, static}]) and
    ``properties`` ([{name, type, value, error, static}], value None if not
    evaluated), ``methods`` ([{name, params, returns, owner}], None if not
    listed) and ``methods_error``.
    """
    t = translations

    def esc(text):
        return html.escape("" if text is None else str(text))

    title = snapshot.get("class_name") or "?"
    if snapshot.get("instance_name"):
        title += f" \u201c{snapshot['instance_name']}\u201d"
    facts = [(t["inspector.export_address"], snapshot.get("address"))]
    if snapshot.get("length") is not None:
        facts.append((t["inspector.export_length"], snapshot["length"]))
    program = snapshot.get("program")
    if program:
        facts.append((t["inspector.export_program"],
                      t["app.process_display"].format(name=program["name"], pid=program["pid"])))
        if program.get("path"):
            facts.append((t["inspector.export_path"], program["path"]))
    facts.append((t["inspector.export_time"], snapshot["time"].strftime("%Y-%m-%d %H:%M:%S")))
    if snapshot.get("captured"):
        facts.append((t["inspector.export_values"],
                      t["inspector.captured"].format(time=snapshot["captured"])))

    def table_head(kind):
        columns = _COLUMNS[kind]
        return ["<table>", "<colgroup>",
                *(f'<col style="width: {width}">' if width else "<col>"
                  for _key, width in columns),
                "</colgroup>",
                "<tr>" + "".join(f"<th>{esc(t[key])}</th>" for key, _width in columns) + "</tr>"]

    out = [
        "<!DOCTYPE html>",
        f'<html lang="{esc(language)}">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{esc(title)} @ {esc(snapshot.get('address'))}</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        f"<h1>{esc(title)}</h1>",
        "<dl>",
        *(f"<dt>{esc(label)}</dt><dd>{esc(value)}</dd>" for label, value in facts),
        "</dl>",
        f"<h2>{esc(t['inspector.fields'])}</h2>",
    ]
    fields = snapshot.get("fields", [])
    if fields:
        out += table_head("fields")
        for field in fields:
            row_class = ' class="static"' if field.get("static") else ""
            out.append(
                f"<tr{row_class}><td>{esc(field.get('name'))}</td>"
                f'<td class="type">{esc(field.get("type"))}</td>'
                f'<td class="value">{esc(field.get("value"))}</td>'
                f'<td class="offset">{esc(field.get("offset"))}</td>'
                f'<td class="address">{esc(field.get("address"))}</td></tr>')
        out.append("</table>")
    else:
        out.append(f"<p>{esc(t['inspector.export_none'])}</p>")

    properties = snapshot.get("properties", [])
    if properties:
        out += [f"<h2>{esc(t['inspector.properties'])}</h2>", *table_head("properties")]
        for prop in properties:
            row_class = ' class="static"' if prop.get("static") else ""
            value = prop.get("value")
            if value is None:
                cell = f'<td class="value">{esc(t["inspector.export_not_evaluated"])}</td>'
            else:
                css = "value error" if prop.get("error") else "value"
                cell = f'<td class="{css}">{esc(value)}</td>'
            out.append(f"<tr{row_class}><td>{esc(prop.get('name'))}</td>"
                       f'<td class="type">{esc(prop.get("type"))}</td>{cell}</tr>')
        out.append("</table>")

    methods = snapshot.get("methods")
    if methods or snapshot.get("methods_error"):
        out.append(f"<h2>{esc(t['inspector.methods'])}</h2>")
    if snapshot.get("methods_error"):
        out.append('<p class="error">' + esc(t["inspector.methods_failed"].format(
            message=snapshot["methods_error"])) + "</p>")
    elif methods:
        out += table_head("methods")
        for method in methods:
            out.append(f"<tr><td>{esc(method.get('name'))}</td>"
                       f'<td class="value">{esc(method.get("params"))}</td>'
                       f'<td class="type">{esc(method.get("returns"))}</td>'
                       f'<td class="owner">{esc(method.get("owner"))}</td></tr>')
        out.append("</table>")

    out += [f"<footer>{esc(t['inspector.export_footer'].format(version=snapshot.get('version', '')))}"
            "</footer>", "</body>", "</html>", ""]
    return "\n".join(out)
