# Notes Canvas for Open WebUI

Turns Open WebUI **Notes** into an editor in the style of **ChatGPT Canvas**. You write the document on one
side and chat with the model on the other. The model edits **only the right passage**, shows what changed in
red and green, and keeps a history so you can undo. **It works with any model** that supports tool calling.

> **Why it exists:** the built-in note editing tool (`replace_note_content`) asks the model for *character
> offsets* ("from 4,817 to 5,203"). Models cannot count characters, so edits miss, or the model ends up
> deleting and rewriting the whole note. Notes Canvas finds the passage **by its own text**.

This is not a fork. It runs on a regular Open WebUI install (tested on **0.11.4**). Everything runs inside
your own Open WebUI: no external services and no extra packages.

---

## How it works (two pieces)

1. **`Notes Canvas` tool** (`src/notes_canvas.py`): the safe editing functions.
   - Two of them share **the names of the built-in ones**: `view_note` and `replace_note_content`.
   - When Notes Canvas is enabled, Open WebUI uses its versions instead of the built-ins (a built-in tool is
     only added if its name is free).
   - So the note chat's own instruction ("read with `view_note`, edit with `replace_note_content`") leads
     **any model** to the safe editor.
2. **`Notes Canvas Activator` filter** (`src/notes_canvas_activator.py`): a *filter* is code that adjusts every
   request before it reaches the model.
   - It notices when you are in the chat next to a note and **turns the tool on by itself**.
   - It adds a few short rules ("change only what was asked", "don't repeat the text").
   - You don't need to enable anything by hand or use a special model.

---

## Features

| Function (called by the model) | What for |
|---|---|
| `view_note` | Reads the whole note or a single section, which saves tokens. *Replaces the built-in.* |
| `replace_note_content` | Replaces **only the given passages**, found by their text. **The text you selected in the editor** works as-is, even without its formatting. Several edits in one call become a single version. *Replaces the built-in.* |
| `insert_text` | Adds text at the end of the note or after a passage, **without touching what already exists**. Use it to keep writing. |
| `suggest_edits` | **Proposes** changes without applying them. They appear numbered in a card, each with before/after, a comment and a checkbox. |
| `apply_suggestions` / `dismiss_suggestions` | Applies the checked ones (or all) from the stored text, so the model rewrites nothing. Or dismisses them. |
| `append_to_section` / `replace_section` | Works on a Markdown section (e.g. "Summaries", "Current status"). |
| `undo_last_edit` | Goes back to the previous state. |
| `show_history` / `restore_version` | Lists the **last 10 versions** and restores any of them. |
| `open_canvas` | From a regular chat, creates or reopens a note and replies with its link. |

**Safeguards:**
- **Guard against wiping the note:** any edit that would discard more than half of the text is **refused**,
  unless the model confirms you explicitly asked for it. Even then, the previous version stays in the history.
- **Only the note you are on:** in the chat next to a note, only that note can be changed. Other notes stay
  readable, but an edit aimed at them is refused, because models sometimes "fix" look-alike notes too. You
  can turn this off with the tool's `lock_to_current_note` valve.
- **All or nothing:** if one edit in a batch fails, none is applied.
- **Diff card on every edit:** only you see it; the model gets a short summary, which saves tokens. It has
  **Undo** and **History** buttons, which only fill in the message box.

---

## Installation

1. **Tool:** *Workspace → Tools → +*. Delete the example, paste `src/notes_canvas.py` and **Save**.
   Or, as an admin, use **Import From Link** in the same menu with the file's GitHub URL.
   - Keep the id `notes_canvas`. If you use another id, the filter finds the tool anyway.
   - **Multi-user instances:** also give your users **read access** to the tool (its *Access* setting). Open
     WebUI drops tools a user can't access.
2. **Filter:** *Admin Panel → Functions → +*. Paste `src/notes_canvas_activator.py` (or import it from its
   link) and **Save**.
   - In the Functions list, **enable** it.
   - In its menu (⋯), turn on **Global**. Without Global, it doesn't run.
3. **Remove or disable** other note-editing tools, so the model isn't confused.
4. **"/" shortcuts (optional):** for each file in `src/prompts/`, go to *Workspace → Prompts → +*. Fill in
   **Title**, **Command** and **Content** as shown in the file.
5. **Use:** open a note, open the chat next to it (speech-bubble icon), pick **any model** and ask for changes.
   To change a specific passage, **select it in the editor** first.

The model must support tool calling in **Native** mode, which is Open WebUI's default.

### Updating

Paste the new code over the old one in the same tool and function, and save. Cards already in old chat
messages keep the code they were created with, so check an update in a **new** chat.

---

## Using it

In the chat next to a note, just talk to the model:

| You say | What happens |
|---|---|
| *(select a sentence)* "make this more formal" | Only that sentence changes. A card shows the diff, with **Undo** and **History**. |
| "fix the typos in the second paragraph and bold the title" | Several edits in one call, saved as a single version. |
| `/suggest-edits` | A card with numbered suggestions. Uncheck the ones you don't want, click **Apply checked** and press Enter. |
| "keep writing, two more paragraphs" | New text goes at the end; nothing else is touched. |
| "undo" · "show the history" · "restore version 3" | Goes back, lists the versions, restores one. |
| *(in a regular chat)* "open a canvas called Trip plan" | Creates or reopens the note and replies with its link. |

### Shortcuts

| Command | Does | ChatGPT Canvas |
|---|---|---|
| `/suggest-edits` | Up to 8 commented suggestions for you to pick from. | Suggest edits |
| `/review-selection` | Up to 5 alternatives for the selected passage only. | — |
| `/shorten` · `/lengthen` | About 30% shorter or longer. | Adjust the length |
| `/reading-level` | Rewrites for a layperson / high school / college / expert reader (edit the brackets). | Reading level |
| `/final-polish` | Grammar, punctuation and consistency, with minimal changes. | Add final polish |
| `/keep-writing` | Continues the text in the same voice, without touching what exists. | — |

They all act on the **selected passage** or, with no selection, on the whole note.

### Tool settings

Open them in *Workspace → Tools → Notes Canvas → ⚙ Valves*:

| Valve | Default | Effect |
|---|---|---|
| `lock_to_current_note` | on | In a note's chat, only that note can be changed. Turn it off to let a note chat edit other notes too (e.g. a running summary kept in another note). |

### Filter settings

Open them in *Functions → Notes Canvas Activator → ⚙ Valves*:

| Valve | Default | Effect |
|---|---|---|
| `tool_id` | `notes_canvas` | Id the tool was saved with. If no tool has this id, it is looked up by its functions. |
| `mode` | `notes` | `notes`: Notes Canvas only in the chat next to a note. `always`: in every chat, so a regular chat can safely edit notes too. `always` costs a few extra tokens per message. |
| `rules` | on | Appends the short editing rules in note chats, including the id of the chat's note. |

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| The model answers with a plain-text list or rewrites the note; no cards | The filter is not running. In *Admin Panel → Functions*, the activator must be both **enabled** (toggle on) and **Global**. |
| Works for admins but not for other users | Users need **read access** to the tool (the tool's *Access* setting). |
| The model never calls any tool | Use a model that supports tool calling, with *Function Calling* set to **Native** (the default). |
| "Passage not found" | The note changed or the passage was paraphrased. The model gets similar lines back and usually retries on its own. |
| "This chat is attached to the note…" | The model tried to change another note from a note chat. That's the `lock_to_current_note` valve at work. |

---

## Limitations

These can't be done without changing Open WebUI itself:

- Text doesn't "type itself" into the editor: each edit is saved at once.
- The red/green highlight is in the **chat card**, not inside the editor.
- From a regular chat, the canvas opens through a link, not in a side panel.

**Data:** history and pending suggestions are stored in each note's `meta` field, under the `notes_canvas`
key. The note text itself is saved exactly the way the built-in tool saves it.

---

## Project structure

| Path | What it is |
|---|---|
| `src/notes_canvas.py` | **The tool.** Paste it in *Workspace → Tools*. |
| `src/notes_canvas_activator.py` | **The filter.** Paste it in *Admin Panel → Functions*. |
| `src/prompts/` | Canvas-style "/" shortcuts: one file per shortcut, with title, command and content. |
| `tests/test_logic.py` | Fast logic tests against a **fake** notes database. |
| `tests/test_integration.py` | Tests against the **real** Open WebUI code, in a throwaway database under `tests/tmp/`. |

## Tests

Run from the project folder, using the Python of an Open WebUI install:

```bash
python tests/test_logic.py
```

```bash
python tests/test_integration.py
```

The second one needs the `open_webui` package installed in that Python. It recreates a throwaway database in
`tests/tmp/`, which Git ignores.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
