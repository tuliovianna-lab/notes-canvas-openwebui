"""
title: Notes Canvas
author: Túlio Vianna
author_url: https://github.com/tuliovianna-lab/notes-canvas-openwebui
version: 1.1.0
license: MIT
required_open_webui_version: 0.11.0
description: Turns Open WebUI Notes into a ChatGPT-Canvas-style editor that works with any model. The model edits only the right passage, found by its own text (your editor selection works as-is, even across formatting), or proposes numbered suggestions you pick from. Every change shows a red/green diff card with Undo, and the last 10 versions can be restored. Pair it with the "Notes Canvas Activator" filter.
"""

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
# The built-in `replace_note_content` edits by CHARACTER OFFSETS, which models
# cannot count reliably: edits miss, or the model ends up rewriting the whole
# note. Here the model points at the passage by its TEXT (the "anchor"). If the
# text does not match exactly - e.g. because it came from the editor selection,
# which arrives without `**`, `#` or list markers - the tool compares the
# *visible text* of the Markdown and still finds the passage.
#
# Two functions deliberately reuse the built-in names (view_note and
# replace_note_content): when this tool is enabled, Open WebUI uses them
# instead of the built-ins, so the note chat's own system instruction leads
# ANY model to the safe editor.
#
# File layout (top to bottom):
#   1. Text and Markdown sections (pure functions, testable without a DB)
#   2. Formatting-tolerant anchor (pure functions)
#   3. HTML cards (diffs, suggestions, history) - shown to the user only
#   4. Open WebUI integration (notes DB, permissions, live editor refresh)
#   5. Tools class: the functions the model sees

import difflib
import json
import re
import time
import unicodedata
from typing import Optional

from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

META_KEY = "notes_canvas"  # where history and suggestions live, inside note.meta
MAX_VERSIONS = 10

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


class EditError(Exception):
    """Error whose message is written for the model to read and fix its call."""


# ===========================================================================
# 1. Text and Markdown sections
# ===========================================================================


def normalize_title(title: str) -> str:
    """Makes a heading comparable: no '#', no emphasis, no accents, lowercase."""
    t = title.strip().lstrip("#").strip()
    t = re.sub(r"[*_`]", "", t)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip(" :").lower()


def map_sections(md: str) -> list[dict]:
    """
    Lists the Markdown headings with their positions. Each section runs from
    its heading to the next heading of the same or higher level (so it
    includes its subsections). '#' inside code blocks is ignored.
    """
    sections = []
    pos = 0
    in_code = False
    for line in md.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        if bare.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        elif not in_code:
            m = _HEADING_RE.match(bare)
            if m:
                sections.append({"level": len(m.group(1)), "title": m.group(2).strip(), "start": pos, "body": pos + len(line)})
        pos += len(line)

    for i, section in enumerate(sections):
        section["end"] = len(md)
        for following in sections[i + 1 :]:
            if following["level"] <= section["level"]:
                section["end"] = following["start"]
                break
    return sections


def outline(sections: list[dict]) -> str:
    if not sections:
        return "(the note has no Markdown headings)"
    top = min(s["level"] for s in sections)
    return "\n".join(f"{'  ' * (s['level'] - top)}- {'#' * s['level']} {s['title']}" for s in sections)


def _pick_unique(items: list, key, target: str, describe, label: str):
    """Tries equality, then prefix, then substring; the match must be unique."""
    for criterion in (lambda t: t == target, lambda t: t.startswith(target), lambda t: target in t):
        found = [i for i in items if criterion(key(i))]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            listing = "\n".join(describe(i) for i in found[:10])
            raise EditError(f"More than one {label} matches. Use the full name or the id:\n{listing}")
    return None


def find_section(md: str, section: str) -> dict:
    sections = map_sections(md)
    target = normalize_title(section)
    if not target:
        raise EditError("Give the section title.")
    found = _pick_unique(
        sections, lambda s: normalize_title(s["title"]), target, lambda s: f"- {'#' * s['level']} {s['title']}", "section"
    )
    if not found:
        raise EditError(f'Section "{section}" not found. Existing sections:\n{outline(sections)}')
    return found


def join_blocks(before: str, block: str, after: str) -> str:
    """Glues three pieces with exactly one blank line between them."""
    parts = [p for p in (before.rstrip("\n"), block.strip("\n"), after.lstrip("\n")) if p]
    return "\n\n".join(parts).rstrip("\n") + "\n"


def append_in_section(md: str, section: str, text: str, position: str = "end") -> str:
    if not text.strip():
        raise EditError("The text to append is empty.")
    s = find_section(md, section)
    position = (position or "end").strip().lower()
    if position == "start":
        point = s["body"]
    elif position == "end":
        point = s["end"]
    else:
        raise EditError('position must be "start" or "end".')
    return join_blocks(md[:point], text, md[point:])


def replace_section_body(md: str, section: str, new_content: str) -> str:
    s = find_section(md, section)
    lines = new_content.strip("\n").splitlines()
    # If the model repeated the section's own heading, drop it to avoid a duplicate.
    if lines and _HEADING_RE.match(lines[0]) and normalize_title(lines[0]) == normalize_title(s["title"]):
        new_content = "\n".join(lines[1:])
    return join_blocks(md[: s["body"]], new_content, md[s["end"] :])


def section_text(md: str, section: str) -> str:
    s = find_section(md, section)
    return md[s["start"] : s["end"]].rstrip("\n")


# ===========================================================================
# 2. Formatting-tolerant anchor
# ===========================================================================
# "Projecting" the Markdown = producing only the text a reader sees (no **, #,
# list markers, link syntax or escape backslashes), with whitespace collapsed
# to single spaces, plus a map saying which original position each character
# came from. The anchor goes through the same projection; the projected anchor
# is searched in the projected note, and the map leads back to the real
# positions in the Markdown.

_BLOCK_PREFIX = re.compile(
    r"^[ \t]*(?:>[ \t]?)*(?:#{1,6}[ \t]+|(?:[-*+]|\d{1,9}[.)])[ \t]+(?:\[[ xX]\][ \t]+)?)?", re.MULTILINE
)
_LINK = re.compile(r"!?\[([^\]\n]*)\]\([^)\n]*\)")
_ESCAPABLE = set("\\`*_{}[]()#+-.!|>~<\"'")
_TYPOGRAPHY = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "\u00a0": " ", "\u202f": " "})


def _syntax_mask(md: str) -> list[bool]:
    """Flags (True) the characters that are Markdown syntax, not visible text."""
    skip = [False] * len(md)

    for m in _BLOCK_PREFIX.finditer(md):
        for k in range(m.start(), m.end()):
            skip[k] = True

    for m in _LINK.finditer(md):
        for k in range(m.start(), m.start(1)):  # "[" or "!["
            skip[k] = True
        for k in range(m.end(1), m.end()):  # "](url)"
            skip[k] = True

    n = len(md)
    for k, c in enumerate(md):
        if c in "*`":
            skip[k] = True
        elif c == "~" and ((k > 0 and md[k - 1] == "~") or (k + 1 < n and md[k + 1] == "~")):
            skip[k] = True
        elif c == "_" and not (0 < k < n - 1 and md[k - 1].isalnum() and md[k + 1].isalnum()):
            skip[k] = True

    k = 0
    while k < n - 1:  # "\*" -> the backslash goes, the asterisk becomes text
        if md[k] == "\\" and md[k + 1] in _ESCAPABLE:
            skip[k] = True
            skip[k + 1] = False
            k += 2
        else:
            k += 1
    return skip


def project(md: str) -> tuple[str, list[int]]:
    """Visible text of the Markdown + map (visible position -> original position)."""
    skip = _syntax_mask(md)
    visible: list[str] = []
    positions: list[int] = []
    for k, c in enumerate(md):
        if skip[k]:
            continue
        c = c.translate(_TYPOGRAPHY)
        if c.isspace():
            if visible and visible[-1] != " ":
                visible.append(" ")
                positions.append(k)
            continue
        visible.append(c)
        positions.append(k)
    while visible and visible[-1] == " ":
        visible.pop()
        positions.pop()
    return "".join(visible), positions


def _count_marker(text: str, marker: str) -> int:
    if len(marker) == 2:
        return text.count(marker)
    # single marker: count only standalone occurrences (not the ones inside "**")
    return len(re.findall(rf"(?<!{re.escape(marker)}){re.escape(marker)}(?!{re.escape(marker)})", text))


_MARKERS = ("**", "__", "~~", "`", "*", "_")


def balance_markup(md: str, start: int, end: int) -> tuple[int, int, str, str]:
    """
    Avoids breaking bold/italic/code when the passage cuts a markup in half.

    1. If the orphan marker touches the edge, widen the passage to include it.
    2. Otherwise the formatting continues outside the passage: return the
       marker that must CLOSE right before it (e.g. "**very** " + new) or
       REOPEN right after it (e.g. new + " **rest**"). The passage only grows
       over neighbouring spaces, so the marker sits next to the text.

    Returns (start, end, prefix, suffix): md[start:end] is replaced by
    prefix + new_text + suffix.
    """
    for _ in range(3):
        changed = False
        for mk in _MARKERS:
            if _count_marker(md[start:end], mk) % 2 == 1:
                if md[max(0, start - len(mk)) : start] == mk:
                    start -= len(mk)
                    changed = True
                elif md[end : end + len(mk)] == mk:
                    end += len(mk)
                    changed = True
        if not changed:
            break

    prefix = suffix = ""
    for mk in _MARKERS:
        if _count_marker(md[start:end], mk) % 2 == 0:
            continue
        line_start = md.rfind("\n", 0, start) + 1
        if _count_marker(md[line_start:start], mk) % 2 == 1:
            # the markup opened before the passage: close it before the passage
            before = md[:start].rstrip(" \t")
            spaces = md[len(before) : start]
            if before.endswith(mk):  # would leave an empty "****": drop the pair
                start, prefix = len(before) - len(mk), spaces + prefix
            else:
                start, prefix = len(before), mk + spaces + prefix
        else:
            # the markup closes after the passage: reopen it after the passage
            rest = md[end:]
            stripped = rest.lstrip(" \t")
            spaces = rest[: len(rest) - len(stripped)]
            if stripped.startswith(mk):
                end, suffix = end + len(spaces) + len(mk), suffix + spaces
            else:
                end, suffix = end + len(spaces), suffix + spaces + mk
    return start, end, prefix, suffix


def locate_by_visible_text(md: str, anchor: str) -> Optional[tuple[int, int, str, str]]:
    """
    Finds the anchor comparing visible text only. Returns (start, end, prefix,
    suffix) in the original Markdown (see balance_markup), or None if not
    found. The match must be unique.
    """
    target = project(anchor)[0].strip()
    if not target:
        return None
    text, positions = project(md)
    hits = []
    p = text.find(target)
    while p != -1:
        hits.append(p)
        p = text.find(target, p + 1)
    if not hits:
        return None
    if len(hits) > 1:
        raise EditError(
            f"The passage appears {len(hits)} times in the note. Include more surrounding text "
            "(the previous or next sentence) to make it unique."
        )
    start = positions[hits[0]]
    end = positions[hits[0] + len(target) - 1] + 1
    return balance_markup(md, start, end)


def not_found_hint(md: str, passage: str) -> str:
    first = next((l for l in passage.splitlines() if l.strip()), passage).strip()[:300]
    lines = [l for l in md.splitlines() if l.strip()]
    similar = difflib.get_close_matches(first, lines, n=2, cutoff=0.5)
    if not similar:
        return "No similar line. Read the note again with view_note and copy the passage."
    examples = "\n".join(f"  «{l}»" for l in similar)
    return f"Similar lines in the note (copy from them):\n{examples}"


def locate(md: str, old: str, replace_all: bool = False) -> list[tuple[int, int, str, str]]:
    """
    Returns the spans to replace as (start, end, prefix, suffix). Tries, in order:
      1. the exact text;  2. exact text without line breaks at the ends;
      3. visible text (tolerates formatting, whitespace and curly quotes).
    """
    if not old or not old.strip():
        raise EditError('"old" is empty. Copy the passage that should be replaced.')

    for candidate in dict.fromkeys((old, old.strip("\n"), old.strip())):
        if not candidate:
            continue
        count = md.count(candidate)
        if count == 0:
            continue
        if count > 1 and not replace_all:
            raise EditError(
                f"The passage appears {count} times in the note. Include more surrounding text "
                "(the previous or next sentence) to make it unique."
            )
        spans, p = [], md.find(candidate)
        while p != -1:
            spans.append((p, p + len(candidate), "", ""))
            if not replace_all:
                break
            p = md.find(candidate, p + len(candidate))
        return spans

    found = locate_by_visible_text(md, old)
    if found:
        return [found]
    raise EditError("Passage not found in the note. " + not_found_hint(md, old))


def apply_replacements(
    md: str, replacements: list[tuple[str, str]], replace_all: bool = False
) -> tuple[str, int, list[str]]:
    """
    Applies several replacements (old -> new) at once, all or nothing. Every
    anchor is searched in the ORIGINAL text; spans cannot overlap.
    Returns (new text, number of replacements, warnings for the model).
    """
    if not replacements:
        raise EditError("No edits given.")
    targets = []
    for n, (old, new) in enumerate(replacements, start=1):
        try:
            for start, end, prefix, suffix in locate(md, old, replace_all):
                targets.append((start, end, prefix + new + suffix, n))
        except EditError as e:
            raise EditError(f"Edit {n}: {e}") from None

    targets.sort()
    for (s1, e1, _, n1), (s2, _, _, n2) in zip(targets, targets[1:]):
        if s2 < e1:
            raise EditError(f"Edits {n1} and {n2} touch the same passage. Merge them into one.")

    warnings = []
    for start, end, new, n in targets:
        breaks = md[start:end].count("\n")
        if breaks and "\n" not in new:
            warnings.append(
                f"edit {n} merged into a single line a passage that had {breaks} line break(s); "
                "if the paragraphs/items should stay separate, redo it with the breaks (\\n) in the new text"
            )

    new_md = md
    for start, end, new, _ in reversed(targets):
        new_md = new_md[:start] + new + new_md[end:]
    return new_md, len(targets), warnings


def replace_with_detail(md: str, replacements: list[tuple[str, str]], replace_all: bool = False) -> tuple[str, str]:
    """apply_replacements + a sentence summarising what was done (the format _edit expects)."""
    new_md, count, warnings = apply_replacements(md, replacements, replace_all)
    detail = f"{count} passage(s) replaced"
    if warnings:
        detail += " - WARNING: " + "; ".join(warnings)
    return new_md, detail


def insert_at(md: str, text: str, after: str = "", as_paragraph: bool = True) -> str:
    """
    Inserts `text` at the end of the note or right after the passage `after`
    (found like the edits are). As a paragraph: separated by a blank line;
    otherwise glued to the passage with a space.
    """
    if not text.strip():
        raise EditError("The text to insert is empty.")
    point = len(md)
    if after.strip():
        _, point, _, _ = locate(md, after)[0]
    if as_paragraph:
        return join_blocks(md[:point], text, md[point:])
    space = "" if (not md[:point] or md[:point][-1].isspace() or text[0].isspace()) else " "
    return md[:point] + space + text + md[point:]


# --- Suggestions (the model proposes, the user picks, the stored text is applied) ---

MAX_SUGGESTIONS = 15


def validate_suggestions(md: str, suggestions) -> tuple[list[dict], list[str]]:
    """
    Checks each suggestion {"old", "new", "comment"} against the current text.
    Returns (valid ones numbered from 1, messages for the ones that failed).
    """
    valid, failed = [], []
    for i, s in enumerate(suggestions or [], start=1):
        if not isinstance(s, dict) or "old" not in s or "new" not in s:
            failed.append(f'Suggestion {i}: use the format {{"old": ..., "new": ..., "comment": ...}}.')
            continue
        try:
            locate(md, str(s["old"]))
        except EditError as e:
            failed.append(f"Suggestion {i}: {e}")
            continue
        valid.append(
            {
                "n": len(valid) + 1,
                "old": str(s["old"]),
                "new": str(s["new"]),
                "comment": str(s.get("comment") or "").strip(),
                "state": "pending",
            }
        )
    if len(valid) > MAX_SUGGESTIONS:
        failed.append(f"Only the first {MAX_SUGGESTIONS} suggestions were stored.")
        valid = valid[:MAX_SUGGESTIONS]
    return valid, failed


def pick_pending(items: list[dict], numbers) -> list[dict]:
    """The requested pending suggestions (all of them if `numbers` is empty)."""
    pending = {s["n"]: s for s in items if s.get("state") == "pending"}
    if not pending:
        raise EditError("There are no pending suggestions for this note. Create new ones with suggest_edits.")
    if not numbers:
        return list(pending.values())
    chosen = []
    for n in numbers:
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise EditError(f"Invalid suggestion number: {n!r}.") from None
        if n not in pending:
            raise EditError(f"Suggestion {n} is not pending. Pending: {', '.join(map(str, sorted(pending)))}.")
        chosen.append(pending[n])
    return chosen


def mark(items: list[dict], chosen: list[dict], state: str) -> list[dict]:
    numbers = {s["n"] for s in chosen}
    return [{**s, "state": state} if s["n"] in numbers else s for s in items]


def list_numbers(items: list[dict]) -> str:
    return ", ".join(str(s["n"]) for s in items) or "none"


# ===========================================================================
# 3. HTML cards (the user sees them; the model gets a short summary)
# ===========================================================================
# WARNING - the card HTML must NOT contain the "&" character:
# Open WebUI stores the card inside a JSON string and, before parsing that
# JSON back, decodes HTML entities (&quot; -> ", &lt; -> <, ...) with the
# html-entities library. A "&quot;" in the card becomes a bare quote, the JSON
# breaks and the card silently disappears. So the HTML is fixed and every
# variable text (note passages, titles, buttons) travels as DATA in a JSON
# where "&", "<" and ">" are written as \u0026, \u003c and \u003e; the card's
# script puts them on screen as plain text (textContent), never as HTML.

_CARD_CSS = """
:root { --bg:#ffffff; --fg:#1f2937; --muted:#6b7280; --border:#e5e7eb;
        --del:#fee2e2; --del-fg:#991b1b; --ins:#dcfce7; --ins-fg:#166534; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#1f2937; --fg:#e5e7eb; --muted:#9ca3af; --border:#374151;
          --del:#7f1d1d66; --del-fg:#fca5a5; --ins:#14532d80; --ins-fg:#86efac; } }
body { margin:0; font:14px/1.55 system-ui, sans-serif; color:var(--fg); background:transparent; }
.card { border:1px solid var(--border); border-radius:12px; padding:10px 14px; background:var(--bg); }
.top { display:flex; justify-content:space-between; gap:8px; font-weight:600; }
.top small { font-weight:400; color:var(--muted); }
.snippet { margin:8px 0; padding:8px 10px; border-left:3px solid var(--border); white-space:pre-wrap; word-break:break-word; }
.ctx { color:var(--muted); }
del { background:var(--del); color:var(--del-fg); border-radius:3px; }
ins { background:var(--ins); color:var(--ins-fg); text-decoration:none; border-radius:3px; }
del + ins { margin-left:3px; }
.more, .note { color:var(--muted); font-size:13px; }
table { border-collapse:collapse; width:100%; font-size:13px; }
td { padding:4px 6px; border-top:1px solid var(--border); }
.actions { margin-top:8px; display:flex; flex-wrap:wrap; gap:6px; }
button { border:1px solid var(--border); background:transparent; color:var(--fg); border-radius:8px;
         padding:3px 10px; cursor:pointer; font:inherit; font-size:13px; }
button:hover { background:#8881; }
.sug { margin:10px 0; }
.sug .head { font-weight:600; cursor:pointer; }
.sug .snippet { margin:4px 0; }
"""

# No "&" here either: no "&&" and no entities. Everything runs inside a
# function so no name can clash with browser globals (e.g. `top`, `name`).
_CARD_JS = """
(function () {
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) { e.className = cls; }
  if (text !== undefined) { e.textContent = text; }
  return e;
}
function checked() {
  var n = [];
  document.querySelectorAll('input.pick').forEach(function (x) { if (x.checked) { n.push(x.value); } });
  return n;
}
function button(b) {
  var x = el('button', '', b.label);
  x.addEventListener('click', function () {
    var text = b.prompt;
    if (b.selection) {
      var n = checked();
      if (!n.length) { return; }
      text = text.replace('{list}', n.join(', '));
    }
    parent.postMessage({ type: 'input:prompt', text: text }, '*');
  });
  return x;
}
function parts(box, list) {
  list.forEach(function (p) {
    box.appendChild(p[0] === 'ctx' ? el('span', 'ctx', p[1]) : el(p[0], '', p[1]));
  });
}
var c = document.getElementById('card');
var header = el('div', 'top');
header.appendChild(el('span', '', D.title));
header.appendChild(el('small', '', D.subtitle));
c.appendChild(header);
(D.snippets || []).forEach(function (s) {
  var d = el('div', 'snippet', '\\u2026');
  parts(d, s);
  d.appendChild(document.createTextNode('\\u2026'));
  c.appendChild(d);
});
if (D.more) { c.appendChild(el('div', 'more', D.more)); }
if (D.note) { c.appendChild(el('div', 'note', D.note)); }
(D.suggestions || []).forEach(function (s) {
  var d = el('div', 'sug');
  var head = el('label', 'head');
  var box = el('input', 'pick');
  box.type = 'checkbox';
  box.value = s.n;
  box.checked = true;
  head.appendChild(box);
  head.appendChild(document.createTextNode(' ' + s.n + '.'));
  d.appendChild(head);
  var t = el('div', 'snippet');
  parts(t, s.parts);
  d.appendChild(t);
  if (s.comment) { d.appendChild(el('div', 'note', s.comment)); }
  c.appendChild(d);
});
if (D.rows) {
  var table = el('table');
  D.rows.forEach(function (row) {
    var tr = el('tr');
    row.forEach(function (v) {
      var td = el('td');
      if (typeof v === 'object') { td.appendChild(button(v)); } else { td.textContent = v; }
      tr.appendChild(td);
    });
    table.appendChild(tr);
  });
  c.appendChild(table);
}
if ((D.buttons || []).length) {
  var actions = el('div', 'actions');
  D.buttons.forEach(function (b) { actions.appendChild(button(b)); });
  c.appendChild(actions);
}
function reportHeight() {
  parent.postMessage({ type: 'iframe:height', height: document.documentElement.scrollHeight }, '*');
}
new ResizeObserver(reportHeight).observe(document.body);
window.addEventListener('load', reportHeight);
reportHeight();
})();
"""


def button(label: str, prompt: str, selection: bool = False) -> dict:
    """
    A button that fills the chat input box (the user just presses Enter).
    With selection=True, "{list}" in the prompt becomes the checked suggestion numbers.
    """
    return {"label": label, "prompt": prompt, "selection": selection}


def _safe_json(data: dict) -> str:
    """JSON with no literal "&", "<" or ">" (see the warning at the top of this section)."""
    return (
        json.dumps(data, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )


def build_card(
    title: str,
    subtitle: str,
    snippets: Optional[list] = None,
    more: str = "",
    note: str = "",
    rows: Optional[list] = None,
    buttons: Optional[list[dict]] = None,
    suggestions: Optional[list[dict]] = None,
) -> HTMLResponse:
    """
    Builds the card from DATA:
      snippets:    list of snippets; each snippet is a list of [kind, text] pairs,
                   kind being "ctx" (context), "del" (removed) or "ins" (added);
      rows:        a table; each cell is a text or a button (dict from button());
      suggestions: [{"n", "parts" (like a snippet), "comment"}], with a checkbox each.
    """
    data = {
        "title": title,
        "subtitle": subtitle,
        "snippets": snippets or [],
        "more": more,
        "note": note,
        "rows": rows or None,
        "buttons": buttons or [],
        "suggestions": suggestions or [],
    }
    page = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<style>{_CARD_CSS}</style></head><body>"
        '<div class="card" id="card"></div>'
        f"<script>var D = {_safe_json(data)};{_CARD_JS}</script></body></html>"
    )
    if "&" in page:  # regression guard: Open WebUI would drop the card
        raise ValueError("card contains a literal '&'")
    return HTMLResponse(content=page, headers={"Content-Disposition": "inline"})


_TOKENS = re.compile(r"\s+|\w+|[^\w\s]", re.UNICODE)


def diff_snippets(before: str, after: str, context: int = 12, max_snippets: int = 8) -> tuple[list, str]:
    """
    Only the pieces that changed, word by word: [["ctx", ...], ["del", ...],
    ["ins", ...]]. Returns (snippets, note about snippets not shown).
    """
    a, b = _TOKENS.findall(before), _TOKENS.findall(after)
    groups = list(difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(context))
    snippets = []
    for group in groups[:max_snippets]:
        parts = []
        for op, i1, i2, j1, j2 in group:
            old, new = "".join(a[i1:i2]), "".join(b[j1:j2])
            if op == "equal":
                parts.append(["ctx", old])
            else:
                if old:
                    parts.append(["del", old])
                if new:
                    parts.append(["ins", new])
        snippets.append(parts)
    more = f"+ {len(groups) - max_snippets} more changed passage(s) not shown" if len(groups) > max_snippets else ""
    return snippets, more


def suggestions_card(note, items: list[dict]) -> HTMLResponse:
    """Numbered list of the pending suggestions, each with before/after and a comment."""
    title = note.title or "Untitled"
    pending = [s for s in items if s.get("state") == "pending"]
    data = [
        {
            "n": s["n"],
            "parts": (diff_snippets(s["old"], s["new"], context=100000, max_snippets=1)[0] or [[]])[0],
            "comment": s.get("comment", ""),
        }
        for s in pending
    ]
    return build_card(
        f"💡 Suggestions — {title}",
        f"{len(pending)} pending · nothing applied yet",
        note="" if data else "No pending suggestions.",
        suggestions=data,
        buttons=[
            button("✔ Apply checked", f'Apply suggestions {{list}} to the note "{title}".', selection=True),
            button("✖ Dismiss all", f'Dismiss the pending suggestions of the note "{title}".'),
        ],
    )


def change_card(note, before: str, after: str, action: str, versions: int) -> HTMLResponse:
    title = note.title or "Untitled"
    snippets, more = diff_snippets(before, after)
    return build_card(
        f"✏️ {title}",
        f"{action} · {time.strftime('%H:%M')} · {versions} version(s) kept",
        snippets=snippets,
        more=more,
        note="" if snippets else "No difference in the text.",
        buttons=[
            button("↩ Undo", f'Undo the last edit of the note "{title}".'),
            button("🕘 History", f'Show the history of the note "{title}".'),
        ],
    )


# ===========================================================================
# 4. Open WebUI integration
# ===========================================================================


def _note_md(note) -> str:
    return (((note.data or {}).get("content") or {}).get("md")) or ""


def _canvas_meta(note) -> dict:
    """Everything this tool stores in the note (history and suggestions), in note.meta["notes_canvas"]."""
    return dict((note.meta or {}).get(META_KEY) or {})


def _versions(note) -> list[dict]:
    return list(_canvas_meta(note).get("versions") or [])


def _suggestions(note) -> list[dict]:
    return [dict(s) for s in _canvas_meta(note).get("suggestions") or []]


async def _can(note, user: dict, permission: str) -> bool:
    if user.get("role") == "admin" or note.user_id == user.get("id"):
        return True
    from open_webui.models.access_grants import AccessGrants
    from open_webui.models.groups import Groups

    groups = [g.id for g in await Groups.get_groups_by_member_id(user.get("id"))]
    return await AccessGrants.has_access(
        user_id=user.get("id"),
        resource_type="note",
        resource_id=note.id,
        permission=permission,
        user_group_ids=set(groups),
    )


async def _resolve_note(ref: str, user: dict, permission: str):
    """Accepts the note id or its title (exact, then prefix, then substring)."""
    from open_webui.models.notes import Notes

    ref = (ref or "").strip()
    if not ref:
        raise EditError("Give the note title or id.")

    if _UUID_RE.match(ref):
        note = await Notes.get_note_by_id(ref)
        if note:
            if not await _can(note, user, permission):
                raise EditError("No permission for this note.")
            return note

    notes = await Notes.get_notes_by_user_id(user.get("id"), permission=permission, limit=None)
    found = _pick_unique(
        notes, lambda n: normalize_title(n.title or ""), normalize_title(ref), lambda n: f"- {n.title} (id: {n.id})", "note"
    )
    if not found:
        raise EditError(f'No note found with the title "{ref}".')
    return found


async def _chat_note_id(chat_id: Optional[str]) -> Optional[str]:
    """Id of the note this chat is attached to (the chat next to a note), or None."""
    if not chat_id:
        return None
    try:
        from open_webui.models.chats import Chats

        chat = await Chats.get_chat_by_id(chat_id)
        meta = (chat.meta or {}) if chat else {}
        if meta.get("internal") is True and meta.get("type") == "note":
            return meta.get("note_id")
    except Exception:
        pass
    return None


async def _target_note(ref: str, user: dict, permission: str, chat_id: Optional[str], lock: bool):
    """
    Resolves the note to work on. In a note's chat, with `lock` on, only that
    note can be used: a missing or ambiguous reference falls back to it, and a
    reference to ANOTHER note is refused (models sometimes "fix" look-alike
    notes too).
    """
    current = await _chat_note_id(chat_id) if lock else None
    if not current:
        return await _resolve_note(ref, user, permission)
    try:
        note = await _resolve_note(ref, user, permission)
    except EditError:
        note = None
    if note is not None and note.id == current:
        return note
    own = await _resolve_note(current, user, permission)
    if note is None:
        return own
    raise EditError(
        f'This chat is attached to the note "{own.title}" (id: {own.id}); only that note can be changed here. '
        f'Call again with note_id="{own.id}". Do not change other notes from this chat.'
    )


async def _save_meta(note, canvas: dict):
    """Saves only this tool's data (e.g. pending suggestions), without touching the text."""
    from open_webui.models.notes import NoteUpdateForm, Notes

    updated = await Notes.update_note_by_id(note.id, NoteUpdateForm(meta={META_KEY: canvas}))
    if not updated:
        raise EditError("Failed to save the note to the database.")
    return updated


async def _save(request, user: dict, note, new_md: str, versions: list[dict], extra: Optional[dict] = None):
    """
    Saves the same way the built-in tool does, so an open editor refreshes
    right away, and keeps the history in note.meta (preserving the rest of
    this tool's data; `extra` overrides keys, e.g. suggestions).
    """
    from open_webui.models.notes import NoteUpdateForm, Notes

    if request is not None:
        try:
            from open_webui.tasks import stop_item_tasks

            await stop_item_tasks(request.app.state.redis, f"note:{note.id}")
        except Exception:
            pass

    content = dict((note.data or {}).get("content") or {})
    form = NoteUpdateForm(
        data={**(note.data or {}), "content": {**content, "json": None, "html": "", "md": new_md}},
        meta={META_KEY: {**_canvas_meta(note), "versions": versions[-MAX_VERSIONS:], **(extra or {})}},
    )
    updated = await Notes.update_note_by_id(note.id, form)
    if not updated:
        raise EditError("Failed to save the note to the database.")

    if request is not None:
        try:
            from open_webui.tools.builtin import _emit_note_updated

            await _emit_note_updated(request, user, updated)
        except Exception:
            pass
    return updated


def check_large_change(before: str, after: str, confirmed: bool) -> None:
    """
    Safety guard: refuses an edit that throws away more than half of the note's
    text, unless the model confirms the user explicitly asked for it.
    """
    if confirmed or len(before) < 400:
        return
    a, b = _TOKENS.findall(before), _TOKENS.findall(after)
    blocks = difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks()
    kept = sum(len("".join(a[x.a : x.a + x.size])) for x in blocks)
    if kept < 0.5 * len(before):
        raise EditError(
            f"This edit would discard {100 - round(100 * kept / len(before))}% of the note's text. "
            "To change only a passage, use `edits` with the exact passage. Only if the user explicitly "
            "asked to rewrite/delete almost everything, repeat the call with confirm_large_change=true."
        )


async def _edit(request, user: dict, ref, action: str, transform, confirmed: bool = False, extra=None):
    """
    Skeleton shared by every edit: resolve the note, compute the new text,
    apply the safety guard, keep the previous version, save, and return
    (card for the user, summary for the model).
    `ref` may be the title/id or the already loaded note.
    """
    note = ref if hasattr(ref, "data") else await _resolve_note(ref, user, "write")
    before = _note_md(note)
    after, detail = transform(before)
    if after == before:
        return f'Nothing changed in the note "{note.title}" (the new text equals the old one).'
    check_large_change(before, after, confirmed)

    versions = _versions(note) + [{"ts": int(time.time()), "action": action, "md": before}]
    updated = await _save(request, user, note, after, versions, extra)
    kept = min(len(versions), MAX_VERSIONS)
    summary = (
        f'OK: {detail} in the note "{updated.title}". The user already sees the diff in a card; '
        "do not repeat the changed text, confirm in one sentence."
    )
    return change_card(updated, before, after, action, kept), summary


def _describe_version(v: dict) -> str:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["ts"]))
    return f"{when} — before {v['action']} ({len(v['md'])} characters)"


def _read_edits(edits) -> list[tuple[str, str]]:
    """Validates the list of edits sent by the model: [{"old": ..., "new": ...}, ...]."""
    replacements = []
    for i, e in enumerate(edits or [], start=1):
        if not isinstance(e, dict) or "old" not in e or "new" not in e:
            raise EditError(f'Edit {i}: use the format {{"old": "current passage", "new": "replacement text"}}.')
        replacements.append((str(e["old"]), str(e["new"])))
    return replacements


# ===========================================================================
# 5. Functions exposed to the model
# ===========================================================================
# view_note and replace_note_content deliberately share the names of the
# built-in Notes tools: when this tool is enabled, Open WebUI uses these
# versions instead (the built-in one is only added if the name is free). The
# note chat's own instruction ("call view_note, then replace_note_content")
# thus leads the model - any model - to the safe, text-anchored editor.


class Tools:
    class Valves(BaseModel):
        lock_to_current_note: bool = Field(
            default=True,
            description="In the chat next to a note, only that note can be changed (other notes stay readable). "
            "Turn off to let a note chat edit other notes too.",
        )

    def __init__(self):
        self.valves = self.Valves()

    async def _note(self, ref: str, user: Optional[dict], permission: str, chat_id: Optional[str]):
        return await _target_note(ref, user or {}, permission, chat_id, self.valves.lock_to_current_note)

    async def view_note(self, note_id: str, section: str = "", __user__: dict = None, __chat_id__: str = None) -> str:
        """
        Reads a note: an outline of its sections and the EXACT text, to copy passages from when editing.
        For a long note, pass `section` to read only that part.
        :param note_id: The note id (or its title).
        :param section: Optional. Title of a Markdown section to read only it, with its subsections.
        :return: Outline and content of the note.
        """
        try:
            try:
                n = await _resolve_note(note_id, __user__ or {}, "read")
            except EditError:
                current = await _chat_note_id(__chat_id__)  # wrong reference in a note chat: read its note
                if not current:
                    raise
                n = await _resolve_note(current, __user__ or {}, "read")
            md = _note_md(n)
            text = section_text(md, section) if section.strip() else md
            return (
                f'Note: "{n.title}" (id: {n.id}) — {len(md)} characters\n'
                f"Sections:\n{outline(map_sections(md))}\n"
                "===== EXACT TEXT =====\n"
                f"{text}\n"
                "===== END ====="
            )
        except EditError as e:
            return f"ERROR: {e}"

    async def replace_note_content(
        self,
        note_id: str,
        edits: Optional[list[dict]] = None,
        full_content: str = "",
        confirm_large_change: bool = False,
        __user__: dict = None,
        __request__=None,
        __chat_id__: str = None,
    ):
        """
        Edits a note by replacing ONLY the given passages, found by their text (not by position).
        Use `edits`: [{"old": "current passage", "new": "replacement text"}]. Several edits in one call = a single version, all or nothing.
        "old" can be copied from view_note or be the text the user selected in the editor, exactly as it came (formatting is tolerated).
        Include enough words for the passage to be unique. "new" may contain Markdown; empty deletes the passage. If the passage spans paragraphs or list items, keep the line breaks (\\n) in "new".
        Only use `full_content` (rewrites the whole note) when the user explicitly asks for it.
        :param note_id: The note id (or its title).
        :param edits: List of replacements {"old": ..., "new": ...}. Passages must not overlap.
        :param full_content: Optional. The complete new text of the note, only for a full rewrite the user asked for.
        :param confirm_large_change: true only if the user explicitly asked to delete or rewrite most of the note.
        :return: Short confirmation, or an error explaining how to fix the call.
        """
        try:
            if edits and full_content:
                raise EditError("Use `edits` OR `full_content`, not both.")
            if full_content:
                transform = lambda md: (full_content, "note fully rewritten")  # noqa: E731
                action = "rewrite the note"
            else:
                replacements = _read_edits(edits)
                if not replacements:
                    raise EditError('Pass `edits`, for example [{"old": "current passage", "new": "new text"}].')
                transform = lambda md: replace_with_detail(md, replacements)  # noqa: E731
                action = "edit a passage" if len(replacements) == 1 else f"{len(replacements)} edits"
            n = await self._note(note_id, __user__, "write", __chat_id__)
            return await _edit(__request__, __user__ or {}, n, action, transform, confirm_large_change)
        except EditError as e:
            return f"ERROR: {e}"

    async def append_to_section(
        self,
        note_id: str,
        section: str,
        text: str,
        position: str = "end",
        __user__: dict = None,
        __request__=None,
        __chat_id__: str = None,
    ):
        """
        Appends a Markdown block inside an existing section, without touching the rest of the note.
        Ideal for growing lists (e.g. a new chapter summary under "Summaries").
        :param note_id: The note id (or its title).
        :param section: Section title (e.g. "Summaries"). "end" is after its subsections.
        :param text: Markdown block to insert (may contain subheadings).
        :param position: "end" (default) or "start" of the section.
        :return: Short confirmation or error.
        """
        try:
            return await _edit(
                __request__,
                __user__ or {},
                await self._note(note_id, __user__, "write", __chat_id__),
                f'append to "{section}"',
                lambda md: (append_in_section(md, section, text, position), f'block appended to "{section}"'),
            )
        except EditError as e:
            return f"ERROR: {e}"

    async def replace_section(
        self,
        note_id: str,
        section: str,
        new_content: str,
        confirm_large_change: bool = False,
        __user__: dict = None,
        __request__=None,
        __chat_id__: str = None,
    ):
        """
        Replaces ALL the content of a section (subsections included), keeping its heading.
        Use only for sections that reflect the latest state (e.g. "Current status"). To change a sentence, use replace_note_content.
        :param note_id: The note id (or its title).
        :param section: Title of the section to replace.
        :param new_content: New body of the section, without repeating the heading.
        :param confirm_large_change: true only if the user explicitly asked to delete or rewrite most of the note.
        :return: Short confirmation or error.
        """
        try:
            return await _edit(
                __request__,
                __user__ or {},
                await self._note(note_id, __user__, "write", __chat_id__),
                f'replace "{section}"',
                lambda md: (replace_section_body(md, section, new_content), f'section "{section}" replaced'),
                confirm_large_change,
            )
        except EditError as e:
            return f"ERROR: {e}"

    async def insert_text(
        self,
        note_id: str,
        text: str,
        after: str = "",
        as_paragraph: bool = True,
        __user__: dict = None,
        __request__=None,
        __chat_id__: str = None,
    ):
        """
        Adds new text without touching the existing text: at the end of the note or right after a passage.
        Use it to keep writing, add a paragraph or complete a list.
        :param note_id: The note id (or its title).
        :param text: Markdown text to insert.
        :param after: Optional. Current passage of the note to insert after (copied literally). Empty = end of the note.
        :param as_paragraph: true (default) inserts a separate paragraph; false glues it right after the passage, on the same line.
        :return: Short confirmation or error.
        """
        try:
            return await _edit(
                __request__,
                __user__ or {},
                await self._note(note_id, __user__, "write", __chat_id__),
                "insert text",
                lambda md: (insert_at(md, text, after, as_paragraph), "text inserted"),
            )
        except EditError as e:
            return f"ERROR: {e}"

    async def suggest_edits(self, note_id: str, suggestions: list[dict], __user__: dict = None, __chat_id__: str = None):
        """
        PROPOSES changes without applying any. The user sees them numbered in a card, checks the ones they want and asks to apply them.
        Use it when the user asks for suggestions, a review or feedback, instead of changing the text directly.
        A new call replaces the previous pending suggestions.
        :param note_id: The note id (or its title).
        :param suggestions: List of {"old": "current passage, copied literally", "new": "proposed text", "comment": "why, in one sentence"}. At most 15.
        :return: How many suggestions were stored and which ones failed.
        """
        try:
            n = await self._note(note_id, __user__, "write", __chat_id__)
            valid, failed = validate_suggestions(_note_md(n), suggestions)
            if not valid:
                return "ERROR: no suggestion could be stored.\n" + "\n".join(failed)
            updated = await _save_meta(n, {**_canvas_meta(n), "suggestions": valid})
            summary = (
                f"{len(valid)} suggestion(s) shown to the user, numbered 1 to {len(valid)}. "
                "Nothing was applied. Do not repeat the suggestions; say in one sentence that they can check the ones they want and ask to apply them."
            )
            if failed:
                summary += "\nNot stored (fix and suggest again, if worthwhile):\n" + "\n".join(failed)
            return suggestions_card(updated, valid), summary
        except EditError as e:
            return f"ERROR: {e}"

    async def apply_suggestions(
        self,
        note_id: str,
        numbers: Optional[list[int]] = None,
        __user__: dict = None,
        __request__=None,
        __chat_id__: str = None,
    ):
        """
        Applies suggestions stored by suggest_edits without rewriting anything: it uses the text already proposed. All or nothing, as a single version.
        :param note_id: The note id (or its title).
        :param numbers: Numbers of the suggestions to apply (e.g. [1, 3]). Empty = all pending ones.
        :return: Short confirmation or error.
        """
        try:
            n = await self._note(note_id, __user__, "write", __chat_id__)
            items = _suggestions(n)
            chosen = pick_pending(items, numbers)
            replacements = [(s["old"], s["new"]) for s in chosen]
            new_items = mark(items, chosen, "applied")
            remaining = [s for s in new_items if s["state"] == "pending"]
            return await _edit(
                __request__,
                __user__ or {},
                n,
                f"apply suggestions {list_numbers(chosen)}",
                lambda md: (
                    replace_with_detail(md, replacements)[0],
                    f"suggestions {list_numbers(chosen)} applied (pending: {list_numbers(remaining)})",
                ),
                extra={"suggestions": new_items},
            )
        except EditError as e:
            hint = " The text changed since the suggestion: create new ones with suggest_edits." if str(e).startswith("Edit ") else ""
            return f"ERROR: {e}{hint}"

    async def dismiss_suggestions(
        self, note_id: str, numbers: Optional[list[int]] = None, __user__: dict = None, __chat_id__: str = None
    ):
        """
        Dismisses pending suggestions (the text does not change).
        :param note_id: The note id (or its title).
        :param numbers: Numbers of the suggestions to dismiss. Empty = all pending ones.
        :return: Short confirmation.
        """
        try:
            n = await self._note(note_id, __user__, "write", __chat_id__)
            items = _suggestions(n)
            chosen = pick_pending(items, numbers)
            new_items = mark(items, chosen, "dismissed")
            await _save_meta(n, {**_canvas_meta(n), "suggestions": new_items})
            remaining = [s for s in new_items if s["state"] == "pending"]
            return f"OK: suggestions {list_numbers(chosen)} dismissed. Pending: {list_numbers(remaining)}. Confirm in one sentence."
        except EditError as e:
            return f"ERROR: {e}"

    async def undo_last_edit(self, note_id: str, __user__: dict = None, __request__=None, __chat_id__: str = None):
        """
        Restores the note to its state before the last edit made by this tool.
        :param note_id: The note id (or its title).
        :return: Short confirmation or error.
        """
        try:
            n = await self._note(note_id, __user__, "write", __chat_id__)
            versions = _versions(n)
            if not versions:
                return "ERROR: there are no previous versions stored for this note."
            last = versions.pop()
            before = _note_md(n)
            updated = await _save(__request__, __user__ or {}, n, last["md"], versions)
            return (
                change_card(updated, before, last["md"], "undo", len(versions)),
                f'OK: undid "{last["action"]}". {len(versions)} version(s) left. Confirm in one sentence.',
            )
        except EditError as e:
            return f"ERROR: {e}"

    async def show_history(self, note_id: str, __user__: dict = None, __chat_id__: str = None):
        """
        Shows the user the previous versions stored by this tool (up to 10), with buttons to restore them.
        :param note_id: The note id (or its title).
        :return: Short list of the versions.
        """
        try:
            n = await self._note(note_id, __user__, "read", __chat_id__)
            versions = _versions(n)
            if not versions:
                return "There are no stored versions for this note yet."
            rows, lines = [], []
            for number, v in enumerate(reversed(versions), start=1):  # 1 = the most recent
                desc = _describe_version(v)
                lines.append(f"{number}. {desc}")
                rows.append([str(number), desc, button("Restore", f'Restore version {number} of the note "{n.title}".')])
            card = build_card(f"🕘 History — {n.title}", f"{len(versions)} version(s)", rows=rows)
            return card, "History shown to the user:\n" + "\n".join(lines) + "\nDo not repeat the list; ask which one to restore, if relevant."
        except EditError as e:
            return f"ERROR: {e}"

    async def restore_version(
        self, note_id: str, number: int, __user__: dict = None, __request__=None, __chat_id__: str = None
    ):
        """
        Restores a version from the history (1 = the most recent). The current state becomes a new version, so it can be reverted.
        :param note_id: The note id (or its title).
        :param number: Version number, as listed by show_history.
        :return: Short confirmation or error.
        """
        try:
            n = await self._note(note_id, __user__, "write", __chat_id__)
            versions = _versions(n)
            if not 1 <= int(number) <= len(versions):
                return f"ERROR: pick a number from 1 to {len(versions)}." if versions else "ERROR: there are no stored versions."
            target = versions[-int(number)]
            before = _note_md(n)
            versions.append({"ts": int(time.time()), "action": f"restore version {number}", "md": before})
            updated = await _save(__request__, __user__ or {}, n, target["md"], versions)
            return (
                change_card(updated, before, target["md"], f"restore version {number}", min(len(versions), MAX_VERSIONS)),
                f"OK: version {number} restored; the previous state was kept in the history. Confirm in one sentence.",
            )
        except (EditError, ValueError) as e:
            return f"ERROR: {e}"

    async def open_canvas(self, title: str, content: str = "", __user__: dict = None):
        """
        Opens a canvas: reuses the note with this title or creates a new one with the initial content.
        Use it when the user asks to write a long document "on a canvas". Then reply with the given link.
        :param title: Note title.
        :param content: Optional. Initial Markdown text (only used if the note does not exist yet).
        :return: Link to the note.
        """
        from open_webui.models.notes import NoteForm, Notes

        user = __user__ or {}
        try:
            notes = await Notes.get_notes_by_user_id(user.get("id"), permission="write", limit=None)
            target = normalize_title(title)
            existing = next((x for x in notes if normalize_title(x.title or "") == target), None)
            if existing:
                n, status = existing, "already existed and was reopened"
            else:
                n = await Notes.insert_new_note(
                    user.get("id"),
                    NoteForm(title=title.strip() or "Canvas", data={"content": {"md": content}}, access_grants=[]),
                )
                status = "was created"
            link = f"/notes/{n.id}"
            card = build_card(
                f"🧾 Canvas: {n.title}",
                status,
                note="Open it from the link in the reply. There, the chat next to the note edits the text.",
            )
            return card, f'The canvas "{n.title}" {status}. Reply only with the Markdown link: [Open "{n.title}"]({link})'
        except EditError as e:
            return f"ERROR: {e}"
