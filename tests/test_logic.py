"""
Logic tests for Notes Canvas, with a FAKE notes database (no running Open WebUI
and no real database needed).

Run from the project folder, with any Python 3.11+ that has fastapi and pydantic
(the Python of your Open WebUI install works):
    python tests/test_logic.py
"""

import asyncio
import html
import importlib.util
import json
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Fake notes database (replaces the Open WebUI modules)
# ---------------------------------------------------------------------------
DB: dict = {}


class FakeNote:
    def __init__(self, id, title, md, user_id="u1"):
        self.id, self.title, self.user_id = id, title, user_id
        self.data = {"content": {"md": md, "html": "<p>x</p>", "json": {}}, "files": ["f1"]}
        self.meta = {}


class FakeForm:
    def __init__(self, **kw):
        self.kw = kw
        self.title = kw.get("title")
        self.data = kw.get("data")


class FakeNotes:
    async def get_note_by_id(self, id):
        return DB.get(id)

    async def get_notes_by_user_id(self, user_id, permission="read", limit=50):
        return [n for n in DB.values() if n.user_id == user_id]

    async def update_note_by_id(self, id, form):
        n = DB[id]
        if "data" in form.kw:
            n.data = {**n.data, **form.kw["data"]}
        if "meta" in form.kw:
            n.meta = {**n.meta, **form.kw["meta"]}
        return n

    async def insert_new_note(self, user_id, form):
        nid = f"{len(DB):08d}-0000-0000-0000-000000000000"
        DB[nid] = FakeNote(nid, form.title, form.data["content"]["md"], user_id)
        return DB[nid]


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m


_stub("open_webui")
_stub("open_webui.models")
_stub("open_webui.models.notes", Notes=FakeNotes(), NoteUpdateForm=FakeForm, NoteForm=FakeForm)
_stub("open_webui.models.groups", Groups=None)
_stub("open_webui.models.access_grants", AccessGrants=None)

CHATS: dict = {}  # chat id -> chat meta


class FakeChats:
    async def get_chat_by_id(self, id):
        return types.SimpleNamespace(meta=CHATS[id]) if id in CHATS else None


_stub("open_webui.models.chats", Chats=FakeChats())

_spec = importlib.util.spec_from_file_location("notes_canvas", ROOT / "src" / "notes_canvas.py")
C = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(C)

# ---------------------------------------------------------------------------
# Tiny checker
# ---------------------------------------------------------------------------
failures = 0


def check(cond, msg, extra=""):
    global failures
    print(("  ok    " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if not cond and extra else ""))
    if not cond:
        failures += 1


def replace(md, old, new):
    return C.apply_replacements(md, [(old, new)])[0]


def error_of(func, *args):
    try:
        func(*args)
    except C.EditError as e:
        return str(e)
    return ""


# Real format saved by the Notes editor (TipTap): "  \n" hard breaks and spaced numbered lists.
EDITOR_NOTE = (
    "[INTRO]  \nThe guide is written in plain English. Each step is **short and direct**.  \n"
    "Avoid technical jargon.  \n[RULES]  \nClarity, yes. Filler, no.\n\n"
    "1.  First list item.\n    \n2.  Second item, with **bold**.\n    \n3.  Third item.\n"
)

PROJECT = """# Project Notes

## Team

### Alice
Designer, 42 hours a week. Remote.

### Bob
Works with Alice.

## Log

### Week 1: Kickoff
Alice drafts the brief.

## Current status
- Alice is in Berlin.

## Open questions
- Who approves the budget?
"""

ID_P = "11111111-2222-3333-4444-555555555555"
ID_LONG = "55555555-2222-3333-4444-555555555555"


def fresh_db():
    DB.clear()
    DB[ID_P] = FakeNote(ID_P, "Project Notes", PROJECT)
    DB["99999999-2222-3333-4444-555555555555"] = FakeNote("99999999-2222-3333-4444-555555555555", "Another note", "text")


def md():
    return DB[ID_P].data["content"]["md"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def tests_projection():
    print("Projection (visible text)")
    txt, positions = C.project("# Title\n\nA **strong** and _light_ [link](http://x.y) \\*literal\\*.")
    check(txt == "Title A strong and light link *literal*.", "removes syntax, keeps the visible text", repr(txt))
    check(len(txt) == len(positions), "map has one position per character")
    txt, _ = C.project("1.  First.\n    \n2.  Second.")
    check(txt == "First. Second.", "editor numbered list becomes running text", repr(txt))
    txt, _ = C.project("snake_case and “quotes” and nbsp")
    check(txt == 'snake_case and "quotes" and nbsp', "underscore inside a word and typography", repr(txt))


def tests_anchor():
    print("Anchor: editor selection (plain text) against the Markdown")
    r = replace(EDITOR_NOTE, "Each step is short and direct.", "Each step is long and slow.")
    check("Each step is long and slow.  \nAvoid" in r and "**" not in r.split("[RULES]")[0], "replaces a passage that contained a whole bold", repr(r[:120]))

    r = replace(EDITOR_NOTE, "short and direct", "brief")
    check("Each step is **brief**." in r, "selection exactly inside the bold keeps the bold (exact text)", repr(r[:120]))

    r = replace(EDITOR_NOTE, "Avoid technical jargon. [RULES] Clarity", "No jargon.  \n[RULES]  \nClarity")
    check("No jargon.  \n[RULES]  \nClarity, yes." in r, "selection across line breaks (new text keeps the breaks)", repr(r[:200]))
    _, _, warnings = C.apply_replacements(EDITOR_NOTE, [("Avoid technical jargon. [RULES] Clarity", "No jargon. [RULES] Clarity")])
    check(len(warnings) == 1 and "line break" in warnings[0], "warns the model when the new text merges lines", str(warnings))

    r = replace(EDITOR_NOTE, "First list item. Second item, with bold.", "Merged item.")
    check("1.  Merged item.\n    \n3.  Third" in r, "selection of two list items", repr(r[-80:]))

    r = replace("She spoke **very slowly** and left.", "slowly and left", "quickly and went")
    check(r == "She spoke **very** quickly and went.", "selection starting inside the bold closes it before", repr(r))

    r = replace("She spoke very **slowly indeed** today.", "very slowly", "quite")
    check(r == "She spoke quite **indeed** today.", "selection ending inside the bold reopens it after", repr(r))

    r = replace("She spoke **slowly** and left.", "spoke slowly", "whispered")
    check(r == "She whispered and left.", "markup touching the edge is included whole", repr(r))

    check("appears 2 times" in error_of(C.apply_replacements, "a **b** c. a _b_ c.", [("a b c", "x")]), "repeated visible text is refused")
    check(replace("a **b** c. a b c.", "a b c", "x") == "a **b** c. x.", "exact text wins over visible text")
    e = error_of(C.apply_replacements, EDITOR_NOTE, [("Third item of the list", "x")])
    check("not found" in e and "Third item" in e, "not found suggests a similar line", e)


def tests_batch():
    print("Several edits at once")
    r, count, _ = C.apply_replacements(PROJECT, [("42 hours", "40 hours"), ("Works with Alice.", "Works closely with Alice.")])
    check(count == 2 and "40 hours" in r and "closely" in r, "applies two edits")
    e = error_of(C.apply_replacements, PROJECT, [("42 hours", "40"), ("Missing passage xyz", "y")])
    check(e.startswith("Edit 2:"), "all or nothing: points at the failing edit", e)
    e = error_of(C.apply_replacements, PROJECT, [("Designer, 42 hours", "a"), ("42 hours a week.", "b")])
    check("same passage" in e, "refuses overlapping edits", e)


def as_open_webui_reads_it(page: str) -> str:
    """
    Reproduces the card's path in the Open WebUI 0.11 interface:
    stringifyAttribute (JSON.stringify of the card list) -> html-entities
    decode() -> parseJSONString. Returns the card as read back, or "" if
    parsing breaks (that is when the card disappears from the screen).
    """
    packed = json.dumps([page], ensure_ascii=False)
    try:
        read = json.loads(html.unescape(packed))
    except json.JSONDecodeError:
        return ""
    return read[0] if isinstance(read, list) and read else ""


def tests_cards():
    print("Cards")
    snippets, _ = C.diff_snippets("The cat <script>alert(1)</script> slept.", "The dog <script>alert(1)</script> slept.")
    check(["del", "cat"] in snippets[0] and ["ins", "dog"] in snippets[0], "word-by-word diff")

    risky = types.SimpleNamespace(title='Note "with quotes" & <b>tags</b>')
    resp = C.change_card(risky, 'He said "hi" & left. <script>x()</script>', 'He said "hello" & left. <script>x()</script>', "edit", 2)
    page = resp.body.decode()
    check("&" not in page, "card has no '&' at all (Open WebUI would break the JSON)")
    check("<script>x()" not in page and "<b>tags" not in page, "note content never becomes live HTML")
    check(as_open_webui_reads_it(page) == page, "survives Open WebUI's real path (JSON + html-entities)")
    check(resp.headers.get("content-disposition") == "inline", "card marked inline (Open WebUI shows it)")

    old_style = '<button data-prompt="Undo &quot;X&quot;">x</button>'
    check(as_open_webui_reads_it(old_style) == "", "reproduces the defect: a card with &quot; disappears (the test catches it)")

    js = C._CARD_JS.strip()
    check(js.startswith("(function () {") and js.endswith("})();"), "card script runs inside its own function (no clash with browser globals)")
    check(not any(f"var {g} " in js for g in ("top", "name", "parent", "self", "status", "length", "frames", "opener")), "card script does not reuse browser global names")

    hist = C.build_card("H", "s", rows=[["1", "desc", C.button("Restore", 'Restore "X".')]])
    check(as_open_webui_reads_it(hist.body.decode()) == hist.body.decode(), "history card survives too")


async def tests_tools():
    t, u = C.Tools(), {"id": "u1", "role": "user"}
    print("Tools")
    fresh_db()
    r = await t.replace_note_content("project", [{"old": "Designer, 42 hours a week.", "new": "Designer, 40 hours a week."}], __user__=u)
    check(isinstance(r, tuple) and "40 hours" in md(), "replace_note_content (ours) saves and returns (card, summary)")
    check("do not repeat" in r[1], "short summary tells the model not to repeat the text")
    note = DB[ID_P]
    check(note.data["content"]["html"] == "" and note.data["content"]["json"] is None, "clears html/json like the built-in")
    check(note.data.get("files") == ["f1"], "keeps the note's attachments")

    r = await t.replace_note_content(ID_P, [{"old": "Remote.", "new": "Fully remote."}, {"old": "Works", "new": "Collaborates"}], __user__=u)
    check(isinstance(r, tuple) and "Fully remote." in md() and "Collaborates with Alice" in md(), "several edits in one call (by id)")
    check(len(C._versions(DB[ID_P])) == 2, "a batch creates a single version (2 in total with the previous one)")

    r = await t.replace_note_content(ID_P, [{"old": "Collaborates"}], __user__=u)
    check(isinstance(r, str) and r.startswith("ERROR") and "format" in r, "validates the edit format")
    r = await t.replace_note_content(ID_P, __user__=u)
    check(isinstance(r, str) and r.startswith("ERROR") and "edits" in r, "no edits: explains how to call it")

    r = await t.view_note(ID_P, section="Team", __user__=u)
    check("EXACT TEXT" in r and "### Bob" in r and "Log" not in r.split("EXACT TEXT")[1], "view_note (ours) reads a single section")

    await t.append_to_section("Project Notes", "Log", "### Week 2\nThe contract arrives.", __user__=u)
    check(md().index("### Week 2") < md().index("## Current status"), "append_to_section at the end of the section")
    await t.replace_section("Project Notes", "Current status", "- Alice is in Lisbon.", __user__=u)
    check("Lisbon" in md() and "Berlin" not in md(), "replace_section")

    r = await t.show_history("Project Notes", __user__=u)
    check(isinstance(r, tuple) and "4." in r[1], "show_history lists 4 versions", r[1] if isinstance(r, tuple) else r)
    check("Restore version 1" in r[0].body.decode(), "show_history has restore buttons")

    before_restore = md()
    await t.restore_version("Project Notes", 4, __user__=u)
    check(md() == PROJECT, "restore_version 4 goes back to the original")
    await t.undo_last_edit("Project Notes", __user__=u)
    check(md() == before_restore, "undo after restore returns to the previous state")

    fresh_db()
    for i in range(13):
        await t.replace_note_content(ID_P, [{"old": "Remote." if i == 0 else f"v{i-1}.", "new": f"v{i}."}], __user__=u)
    check(len(C._versions(DB[ID_P])) == 10, "history capped at 10 versions")
    r = await t.replace_note_content(ID_P, [{"old": "v12.", "new": "v12."}], __user__=u)
    check(isinstance(r, str) and "Nothing changed" in r, "an edit with no change creates no version")

    print("Guard against wiping the note")
    long_md = "# Report\n\n" + "\n\n".join(f"Paragraph {i}: a fairly long text to count." for i in range(20)) + "\n"
    DB[ID_LONG] = FakeNote(ID_LONG, "Long", long_md)
    r = await t.replace_note_content("Long", full_content="Just this.", __user__=u)
    check(isinstance(r, str) and "would discard" in r and DB[ID_LONG].data["content"]["md"] == long_md, "a rewrite that wipes almost everything is refused")
    r = await t.replace_section("Long", "Report", "Nothing.", __user__=u)
    check(isinstance(r, str) and "would discard" in r, "replacing the main section (would wipe everything) is refused")
    r = await t.replace_note_content("Long", [{"old": "Paragraph 3: a fairly long text to count.", "new": "Paragraph 3 revised."}], __user__=u)
    check(isinstance(r, tuple), "a small edit passes the guard")
    r = await t.replace_note_content("Long", full_content="Just this.", confirm_large_change=True, __user__=u)
    check(isinstance(r, tuple) and DB[ID_LONG].data["content"]["md"] == "Just this.", "with explicit confirmation the rewrite passes (and is kept in history)")
    await t.undo_last_edit("Long", __user__=u)
    check("Paragraph 19" in DB[ID_LONG].data["content"]["md"], "and it can be undone")

    r = await t.open_canvas("Draft 3", "# Draft 3\n", __user__=u)
    created = [n for n in DB.values() if n.title == "Draft 3"]
    check(len(created) == 1 and f"/notes/{created[0].id}" in r[1], "open_canvas creates the note and returns the link")
    r = await t.open_canvas("draft 3", __user__=u)
    check(len([n for n in DB.values() if n.title == "Draft 3"]) == 1 and "reopened" in r[1], "open_canvas reuses an existing note")

    DB["77777777-2222-3333-4444-555555555555"] = FakeNote("77777777-2222-3333-4444-555555555555", "Foreign", "x", user_id="u2")
    r = await t.view_note("Foreign", __user__=u)
    check(r.startswith("ERROR"), "does not find another user's note")


async def tests_activator():
    print("Activator filter")
    spec = importlib.util.spec_from_file_location("activator", ROOT / "src" / "notes_canvas_activator.py")
    A = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(A)
    f = A.Filter()

    body = {"messages": [{"role": "system", "content": "CONTEXT:\nCurrent note id: x\nThis chat is attached to the current note.\n"}, {"role": "user", "content": "hi"}]}
    r = await f.inlet(body, __metadata__={})
    check(r.get("tool_ids") == ["notes_canvas"], "note chat: enables the Notes Canvas tool", str(r.get("tool_ids")))
    check("NOTES CANVAS (safe editing" in r["messages"][0]["content"], "note chat: appends the rules")
    r = await f.inlet(r, __metadata__={})
    check(r["tool_ids"] == ["notes_canvas"] and r["messages"][0]["content"].count("NOTES CANVAS (safe editing") == 1, "does not duplicate when run again")

    nid = "abcdef01-2345-6789-abcd-ef0123456789"
    body = {"messages": [{"role": "system", "content": f"CONTEXT:\nCurrent note id: {nid}\nThis chat is attached to the current note.\n"}]}
    r = await f.inlet(body, __metadata__={})
    check(f'note_id="{nid}"' in r["messages"][0]["content"], "tells the model the id of the chat's note")

    plain = {"messages": [{"role": "user", "content": "hi"}], "tool_ids": ["other"]}
    r = await f.inlet(plain, __metadata__={})
    check(r["tool_ids"] == ["other"] and len(r["messages"]) == 1, "regular chat (notes mode): nothing changes")
    f.valves.mode = "always"
    r = await f.inlet({"messages": [{"role": "user", "content": "hi"}]}, __metadata__={})
    check(r.get("tool_ids") == ["notes_canvas"] and len(r["messages"]) == 1, "always mode: enables the tool without the note rules")


def tests_insert():
    print("Insert text")
    check(C.insert_at("A.\n\nB.\n", "C.") == "A.\n\nB.\n\nC.\n", "at the end, as a paragraph")
    check(C.insert_at("A.\n\nB.\n", "New.", after="A.") == "A.\n\nNew.\n\nB.\n", "after a passage, as a paragraph")
    check(C.insert_at("She left.", "and came back.", after="left", as_paragraph=False) == "She left and came back..", "glued to the passage, with a space")
    check("empty" in error_of(C.insert_at, "A.", "  "), "refuses empty text")


async def tests_suggestions():
    t, u = C.Tools(), {"id": "u1", "role": "user"}
    print("Suggestions (propose, pick, apply)")
    fresh_db()
    await t.replace_note_content(ID_P, [{"old": "Remote.", "new": "Fully remote."}], __user__=u)  # creates 1 version
    r = await t.suggest_edits(
        ID_P,
        [
            {"old": "Designer, 42 hours a week.", "new": "Senior designer, 42 hours a week.", "comment": "States the seniority."},
            {"old": "Works with Alice.", "new": "Works closely with Alice.", "comment": "Clarifies the relationship."},
            {"old": "A passage that does not exist", "new": "x"},
            {"old": "Alice drafts the brief.", "new": "Alice writes the brief.", "comment": "More concrete & direct <b>."},
        ],
        __user__=u,
    )
    check(isinstance(r, tuple) and md() == PROJECT.replace("Remote.", "Fully remote."), "suggesting does not change the text")
    items = C._suggestions(DB[ID_P])
    check([s["n"] for s in items] == [1, 2, 3], "stores only the valid ones, numbered 1..3", str(items))
    check("Suggestion 3" in r[1] and "not found" in r[1], "tells the model which suggestion failed", r[1])
    check(len(C._versions(DB[ID_P])) == 1, "storing suggestions keeps the history")
    page = r[0].body.decode()
    check("&" not in page and as_open_webui_reads_it(page) == page, "suggestions card survives Open WebUI")
    check("{list}" in page and '"selection": true' in page, "'Apply checked' button uses the checked boxes")

    r = await t.apply_suggestions(ID_P, [1, 3], __user__=u)
    check(isinstance(r, tuple) and "Senior" in md() and "writes" in md() and "closely" not in md(), "applies only the chosen ones (1 and 3)")
    check("pending: 2" in r[1], "reports the ones still pending", r[1])
    check(len(C._versions(DB[ID_P])) == 2, "applying creates a single version")
    states = {s["n"]: s["state"] for s in C._suggestions(DB[ID_P])}
    check(states == {1: "applied", 2: "pending", 3: "applied"}, "states updated", str(states))

    r = await t.apply_suggestions(ID_P, [1], __user__=u)
    check(isinstance(r, str) and "is not pending" in r, "does not reapply an applied suggestion")
    r = await t.dismiss_suggestions(ID_P, __user__=u)
    check("dismissed" in r and md().count("closely") == 0, "dismissing does not change the text")
    r = await t.apply_suggestions(ID_P, __user__=u)
    check(isinstance(r, str) and "no pending suggestions" in r, "nothing pending: explains")

    await t.undo_last_edit(ID_P, __user__=u)
    check("Senior" not in md() and len(C._suggestions(DB[ID_P])) == 3, "undo keeps the suggestion list")

    await t.suggest_edits(ID_P, [{"old": "Designer, 42 hours a week.", "new": "Designer, 44 hours a week."}], __user__=u)
    await t.replace_note_content(ID_P, [{"old": "Designer, 42 hours a week.", "new": "Designer, retired."}], __user__=u)
    r = await t.apply_suggestions(ID_P, __user__=u)
    check(isinstance(r, str) and "text changed" in r, "a stale suggestion (text changed) is refused with a hint", r)

    r = await t.insert_text(ID_P, "- Who signs the contract?", after="Who approves the budget?", __user__=u)
    check(isinstance(r, tuple) and "Who approves the budget?\n\n- Who signs the contract?" in md(), "insert_text after a passage")


async def tests_lock():
    t, u = C.Tools(), {"id": "u1", "role": "user"}
    print("Note chat edits only its own note")
    fresh_db()
    id_copy = "22222222-2222-3333-4444-555555555555"
    DB[id_copy] = FakeNote(id_copy, "Project Notes (copy)", PROJECT)
    CHATS["c1"] = {"internal": True, "type": "note", "note_id": ID_P}
    CHATS["c2"] = {"tags": []}  # a regular chat
    edit = [{"old": "Remote.", "new": "On site."}]

    r = await t.replace_note_content(id_copy, edit, __user__=u, __chat_id__="c1")
    check(isinstance(r, str) and "only that note" in r and ID_P in r, "editing another note from a note chat is refused", str(r))
    check(DB[id_copy].data["content"]["md"] == PROJECT and md() == PROJECT, "neither note changed")
    r = await t.suggest_edits("Project Notes (copy)", [{"old": "Remote.", "new": "x"}], __user__=u, __chat_id__="c1")
    check(isinstance(r, str) and "only that note" in r, "suggestions for another note are refused too")

    r = await t.replace_note_content("A title the model made up", edit, __user__=u, __chat_id__="c1")
    check(isinstance(r, tuple) and "On site." in md(), "an unknown reference falls back to the chat's note")
    r = await t.view_note("Another made-up title", __user__=u, __chat_id__="c1")
    check(f"(id: {ID_P})" in r, "view_note with a wrong reference reads the chat's note")
    r = await t.view_note(id_copy, __user__=u, __chat_id__="c1")
    check(f"(id: {id_copy})" in r, "other notes stay readable")

    r = await t.replace_note_content(id_copy, edit, __user__=u, __chat_id__="c2")
    check(isinstance(r, tuple) and "On site." in DB[id_copy].data["content"]["md"], "a regular chat can edit any note")

    fresh_db()
    DB[id_copy] = FakeNote(id_copy, "Project Notes (copy)", PROJECT)
    t.valves.lock_to_current_note = False
    r = await t.replace_note_content(id_copy, edit, __user__=u, __chat_id__="c1")
    check(isinstance(r, tuple) and "On site." in DB[id_copy].data["content"]["md"], "with the lock off, a note chat can edit other notes")


def main():
    tests_projection()
    tests_anchor()
    tests_batch()
    tests_cards()
    asyncio.run(tests_tools())
    asyncio.run(tests_activator())
    tests_insert()
    asyncio.run(tests_suggestions())
    asyncio.run(tests_lock())
    print(f"\n{'ALL OK' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
