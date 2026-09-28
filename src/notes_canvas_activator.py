"""
title: Notes Canvas Activator
author: Túlio Vianna
author_url: https://github.com/tuliovianna-lab/notes-canvas-openwebui
version: 1.1.0
license: MIT
required_open_webui_version: 0.11.0
description: Companion filter for the "Notes Canvas" tool. It turns the tool on by itself in note chats, with any model, and adds a few short safe-editing rules. Install it as a Function, then enable it and mark it Global.
"""

# ---------------------------------------------------------------------------
# How it works
# ---------------------------------------------------------------------------
# An Open WebUI *Filter* runs before each request reaches the model and may
# adjust it. This one:
#   1. detects whether the chat is a note chat (the chat next to a Note);
#   2. adds the Notes Canvas tool id to the request's tool list (the list is
#      only read AFTER the filters, so the tool arrives enabled);
#   3. appends a few short editing rules to the system message.
# The Notes Canvas tool has functions with the same names as the built-in
# Notes tools (view_note, replace_note_content), which are thus replaced by
# the safe ones. Result: it works with whatever model you pick.

import re

from pydantic import BaseModel, Field

NOTE_CHAT_MARKER = "This chat is attached to the current note"
NOTE_ID_RE = re.compile(r"Current note id:\s*([0-9a-fA-F-]{32,36})")

# Functions that identify the Notes Canvas tool when looking it up by content.
TOOL_SIGNATURE = {"replace_note_content", "suggest_edits", "apply_suggestions"}

RULES = """
NOTES CANVAS (safe editing of this note):
- To edit, call view_note, then replace_note_content with `edits` = [{"old": ..., "new": ...}]. Copy "old" literally; if the user selected text, use the selection as "old", exactly as it came.
- Change only what was asked. Several changes: a single call with several edits. Never rewrite the whole note unless the user explicitly asks.
- To add text (keep writing, a new paragraph), use insert_text: do not rewrite what already exists.
- If the user asks for suggestions, a review or feedback, use suggest_edits (proposes without applying). "Apply suggestions N": apply_suggestions. "Dismiss": dismiss_suggestions.
- The user sees the diff in a card: do not repeat the changed text or the suggestions; answer in 1 or 2 sentences.
- "Undo", "history" and "restore version N": use undo_last_edit, show_history and restore_version.
""".strip()


def _system_text(body: dict) -> str:
    for m in body.get("messages") or []:
        if m.get("role") == "system":
            c = m.get("content")
            return c if isinstance(c, str) else " ".join(p.get("text", "") for p in c or [] if isinstance(p, dict))
    return ""


def append_rules(body: dict, rules: str) -> None:
    """Appends the rules to the end of the system message (creating one if needed)."""
    messages = body.setdefault("messages", [])
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str):
                if rules not in c:
                    m["content"] = f"{c}\n\n{rules}"
            elif isinstance(c, list):
                c.append({"type": "text", "text": rules})
            return
    messages.insert(0, {"role": "system", "content": rules})


def enable_tool(body: dict, tool_id: str) -> None:
    ids = list(body.get("tool_ids") or [])
    if tool_id not in ids:
        ids.append(tool_id)
    body["tool_ids"] = ids


async def note_of_chat(body: dict, metadata: dict):
    """
    For a note's side chat, returns the note id ("" if unknown); for any other
    chat, None. Recognised by the built-in instruction or, if needed, by the database.
    """
    system = _system_text(body)
    if NOTE_CHAT_MARKER in system:
        m = NOTE_ID_RE.search(system)
        return m.group(1) if m else ""
    chat_id = (metadata or {}).get("chat_id")
    if not chat_id:
        return None
    try:
        from open_webui.models.chats import Chats

        chat = await Chats.get_chat_by_id(chat_id)
        meta = (chat.meta or {}) if chat else {}
        if meta.get("internal") is True and meta.get("type") == "note":
            return meta.get("note_id") or ""
    except Exception:
        pass
    return None


def rules_for(note_id: str) -> str:
    """The rules, plus which note this chat is about (models otherwise pass titles and may pick look-alike notes)."""
    if not note_id:
        return RULES
    return (
        RULES
        + f'\n- This chat is about the note with id "{note_id}": always pass note_id="{note_id}" and change only this note.'
    )


def _function_names(specs) -> set:
    names = set()
    for s in specs or []:
        if isinstance(s, dict):
            names.add(s.get("name") or (s.get("function") or {}).get("name"))
    return names


async def find_tool_id(preferred: str):
    """
    The id the Notes Canvas tool was saved with. Uses `preferred` if that tool
    exists; otherwise looks for a tool exposing the Notes Canvas functions
    (covers installs where the tool got a different id). None if not found.
    """
    try:
        from open_webui.models.tools import Tools

        if preferred and await Tools.get_tool_by_id(preferred):
            return preferred
        for tool in await Tools.get_tools(defer_content=True):
            if TOOL_SIGNATURE <= _function_names(tool.specs):
                return tool.id
    except Exception:
        pass
    return None


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Order among filters (lower runs first).")
        tool_id: str = Field(
            default="notes_canvas",
            description="Id the Notes Canvas tool was saved with. If no tool has this id, it is found automatically.",
        )
        mode: str = Field(
            default="notes",
            description='"notes": enable Notes Canvas only in the chat next to a note. "always": enable it in every chat '
            "(note edits made from any chat are safe too, at the cost of a few extra tokens per message).",
        )
        rules: bool = Field(default=True, description="Append the short editing rules in note chats.")

    def __init__(self):
        self.valves = self.Valves()
        self._resolved: dict = {}  # preferred id -> id found

    async def _tool_id(self) -> str:
        preferred = self.valves.tool_id.strip() or "notes_canvas"
        if preferred not in self._resolved:
            found = await find_tool_id(preferred)
            if not found:  # not installed yet (or lookup failed): try again next time
                return preferred
            self._resolved[preferred] = found
        return self._resolved[preferred]

    async def inlet(self, body: dict, __metadata__: dict = None, __user__: dict = None) -> dict:
        note_id = await note_of_chat(body, __metadata__)
        note_chat = note_id is not None
        if note_chat or self.valves.mode.strip().lower() == "always":
            enable_tool(body, await self._tool_id())
        if note_chat and self.valves.rules:
            append_rules(body, rules_for(note_id))
        return body
