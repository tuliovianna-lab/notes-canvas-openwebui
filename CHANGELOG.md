# Changelog

## 1.1.0

- **Note chats only change their own note.** Models sometimes passed a note title instead of the current note
  id and then edited look-alike notes too. Now, in a note's chat, an edit aimed at another note is refused, and
  a missing or ambiguous reference falls back to the chat's note. Other notes stay readable. The lock can be
  turned off with the tool's `lock_to_current_note` valve.
- The activator tells the model the id of the chat's note.

## 1.0.1

- Fixed: cards were not displayed. The card script reused `top`, a read-only browser global. The script now
  runs in its own scope.

## 1.0.0

- First public release. It includes:
  - text-anchored edits that tolerate formatting and accept the editor selection;
  - suggestions with checkboxes;
  - diff cards with Undo and History;
  - a 10-version history;
  - the activator filter;
  - seven "/" shortcuts.
