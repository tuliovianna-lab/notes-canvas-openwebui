"""
Integration test: runs Notes Canvas against the REAL Open WebUI code, using a
throwaway database in tests/tmp/ (your own data is never touched).

Run from the project folder, with the Python of an Open WebUI install:
    <path-to-open-webui-venv>/python tests/test_integration.py
"""

import asyncio
import importlib.util
import os
import pathlib
import secrets
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = ROOT / "tests" / "tmp"
shutil.rmtree(TMP, ignore_errors=True)  # always start from an empty database
TMP.mkdir(parents=True, exist_ok=True)

# Must come BEFORE importing open_webui: points the data to the throwaway folder.
os.environ["DATA_DIR"] = str(TMP)
os.environ.setdefault("WEBUI_SECRET_KEY", secrets.token_hex(32))

from open_webui.models.notes import NoteForm, Notes  # noqa: E402
from open_webui.models.tools import ToolForm, ToolMeta, Tools  # noqa: E402
from open_webui.utils.plugin import extract_frontmatter  # noqa: E402
from open_webui.utils.tools import get_tool_specs  # noqa: E402

TOOL_PATH = ROOT / "src" / "notes_canvas.py"
FILTER_PATH = ROOT / "src" / "notes_canvas_activator.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = _load("notes_canvas", TOOL_PATH)
A = _load("notes_canvas_activator", FILTER_PATH)

failures = 0


def check(cond, msg, extra=""):
    global failures
    print(("  ok    " if cond else "  FAIL  ") + msg + (f"\n        {extra}" if not cond and extra else ""))
    if not cond:
        failures += 1


def tests_spec():
    print("Open WebUI recognises the tool")
    meta = extract_frontmatter(TOOL_PATH.read_text(encoding="utf-8"))
    check(meta.get("title") == "Notes Canvas" and meta.get("version") and meta.get("license") == "MIT", "frontmatter read", str(meta))
    specs = {s["name"]: s for s in get_tool_specs(C.Tools())}
    expected = {
        "view_note", "replace_note_content", "append_to_section", "replace_section",
        "undo_last_edit", "show_history", "restore_version", "open_canvas",
        "insert_text", "suggest_edits", "apply_suggestions", "dismiss_suggestions",
    }
    check(expected <= set(specs), "every function exposed", str(sorted(specs)))
    ed = specs.get("replace_note_content", {}).get("parameters", {}).get("properties", {}).get("edits", {})
    check("array" in str(ed), "replace_note_content takes a list of edits (array)", str(ed))
    nums = specs.get("apply_suggestions", {}).get("parameters", {}).get("properties", {}).get("numbers", {})
    check("array" in str(nums) and "integer" in str(nums), "apply_suggestions takes a list of numbers", str(nums))
    check(all("__" not in p for s in specs.values() for p in s["parameters"]["properties"]), "internal parameters (__user__ etc.) hidden from the model")
    act = extract_frontmatter(FILTER_PATH.read_text(encoding="utf-8"))
    check(act.get("title") == "Notes Canvas Activator", "activator filter frontmatter read", str(act))
    return list(specs.values())


async def tests_real_db(specs):
    print("With the real notes database (throwaway)")
    t, u = C.Tools(), {"id": "u-test", "role": "user"}
    n = await Notes.insert_new_note(
        "u-test",
        NoteForm(title="Canvas Test", data={"content": {"md": "# Text\n\nEach step is **short and direct**.  \nEnd.\n"}}),
    )
    r = await t.replace_note_content("Canvas Test", [{"old": "Each step is short and direct.", "new": "Each step is long."}], __user__=u)
    current = (await Notes.get_note_by_id(n.id)).data["content"]["md"]
    check(isinstance(r, tuple) and "Each step is long.  \nEnd." in current, "an unformatted selection edit is saved", repr(current))

    r = await t.replace_note_content(n.id, [{"old": "long", "new": "slow"}, {"old": "End.", "new": "Done."}], __user__=u)
    current = (await Notes.get_note_by_id(n.id)).data["content"]["md"]
    check("slow" in current and "Done." in current, "batch saved", repr(current))

    r = await t.show_history("Canvas Test", __user__=u)
    check(isinstance(r, tuple) and "2." in r[1], "history stored in note.meta", r[1] if isinstance(r, tuple) else r)

    r = await t.restore_version("Canvas Test", 2, __user__=u)
    current = (await Notes.get_note_by_id(n.id)).data["content"]["md"]
    check("**short and direct**" in current, "restores the original version", repr(current))

    await t.suggest_edits(n.id, [{"old": "End.", "new": "The real end.", "comment": "More emphatic."}], __user__=u)
    r2 = await t.apply_suggestions(n.id, [1], __user__=u)
    note = await Notes.get_note_by_id(n.id)
    stored = (note.meta or {}).get("notes_canvas", {})
    check(isinstance(r2, tuple) and "The real end." in note.data["content"]["md"], "suggestion stored and applied in the real DB")
    check(stored.get("suggestions", [{}])[0].get("state") == "applied" and len(stored.get("versions", [])) >= 3, "meta keeps suggestions and versions together")

    r = await t.open_canvas("New Draft", "# New Draft\n", __user__=u)
    check(isinstance(r, tuple) and "/notes/" in r[1], "open_canvas creates a real note", r[1] if isinstance(r, tuple) else r)

    r = await t.view_note("Canvas Test", __user__={"id": "someone-else", "role": "user"})
    check(r.startswith("ERROR"), "another user cannot see the note")

    print("Activator finds the tool id")
    check(await A.find_tool_id("notes_canvas") is None, "no tool installed: nothing found")
    await Tools.insert_new_tool(
        "u-test",
        ToolForm(id="my_canvas", name="My Canvas", content=TOOL_PATH.read_text(encoding="utf-8"), meta=ToolMeta()),
        specs,
    )
    check(await A.find_tool_id("notes_canvas") == "my_canvas", "tool saved under another id is found by its functions")
    check(await A.find_tool_id("my_canvas") == "my_canvas", "the configured id is used when it exists")


def main():
    specs = tests_spec()
    asyncio.run(tests_real_db(specs))
    print(f"\n{'ALL OK' if not failures else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
