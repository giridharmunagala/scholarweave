# Instruction-only skills

A skill is one UTF-8 Markdown file describing when and how to accomplish a task with
ScholarWeave's existing tools. It can describe source selection, a sequence of steps, output
format, and when to use a tracked work plan. It cannot add tools, run code, install dependencies,
change providers, or grant permission to save/delete files.

## Use a skill

Describe your task normally, or name the skill in your message:

> Use web-synthesis to condense these three URLs into one readable guide. Save one document
> containing the combined findings and the source links: ...

The model chooses applicable recipes from their instructions, without keyword routing or a
separate classifier call. A skill does not run merely because it is installed. Auto, Quick, and
Thorough all support skills. Thorough workers receive the same recipe snapshot but remain limited
to their assigned track; only the main agent creates the shared work plan.

The bundled `web-synthesis` skill reads one or more webpages, combines their findings, preserves
disagreements and limitations, and includes source titles, URLs, and access dates. It returns the
result in chat unless saving is requested. A saved synthesis is exactly one Markdown workspace
artifact, not a paper folder, raw-page archive, or collection of per-page notes. New documents use
the existing `knowledge/<title--id>.md` location; this feature does not change workspace layout.
Page content remains temporary; the saved document retains its source attribution.

## Write a local skill

In the TUI, open **Skills** with **Ctrl+4** (or `/skills`). Select an entry to view its
instructions, use **Edit / Ctrl+E** to edit or preview, then **Save / Ctrl+S** to save.
**Ctrl+N** creates a new skill. Editing a bundled skill saves a local override without
changing the packaged original. **Revert** discards unsaved edits after confirmation.
Drafts remain intact when switching tabs, refreshing, or encountering a save conflict.
Opening a skill in this tab does not select it for a conversation: the model still decides
which recipes to use from the user's request.

Create `local_data/skills/<skill-name>.md` using a local text editor. With a custom data directory,
use its `skills` subdirectory instead. Create the folder if it does not exist. The TUI creates
this folder automatically when saving a new skill or local override.

The filename is the skill's identifier: lowercase letters, digits, and single hyphens between
words, at most 64 characters before `.md`. Files are read only from this directory's top level.
No frontmatter, manifest, Python module, or registration command is required. For example,
`compare-notes.md` could contain:

```markdown
# Compare notes

Use when the user wants to compare existing local notes on a topic.

1. Clarify the comparison's purpose only if it materially changes the work.
2. Find relevant notes with search_research_notes and read them with read_research_note.
3. For a substantial comparison, use create_work_plan to track reading and synthesis.
   Reuse any active plan with read_work_plan and update_work_item.
4. Compare shared claims, differences, missing evidence, and practical implications.
   Cite the exact note paths; saved notes are not independently verified source evidence.
5. Return a concise comparison in chat. Only use save_research_note if saving was requested.
6. Complete planned items with honest outcomes, or mark genuine blockers with their reasons.
```

Describe applicability in the file, not just the steps. Reference actual tool names and account
for missing capabilities. Respect web-disabled turns, the user's scope, and existing artifact
permissions. Do not require a plan for simple conversation. Do not create another to-do file:
the existing work-plan tools track progress and keep unfinished tasks running across epochs.

Local files replace bundled skills with the same identifier. To restore the bundled version,
remove the local override; to remove a custom skill, remove its file. New, edited, and removed
files take effect on the next message without restarting the server or refreshing the workspace
index. Already-running and recovered runs retain their saved instruction snapshot.

Skills are configuration, not research artifacts: they do not appear in workspace search and
cannot be installed by uploading a chat attachment or saving a research note. Only install
instructions you trust. Applicable provider requests include the skill text, so do not put
credentials or other secrets in these files.

## Limits and scope

This first version includes all installed recipes in the turn's instructions and lets the model
decide which apply. There is no skill-loading tool, manual activation, plugin execution, or remote
installation. Full instructions are snapshotted once for each new chat blueprint;
they are not injected into dedicated paper-summary/OCR runs or the legacy API-only Fast Answer
mode. Skill text consumes part of the selected context window.

Limits are 24 effective skills, 16 KiB per file, and 48 KiB of combined UTF-8 file content after
overrides. Each source directory also allows at most 24 Markdown files. Empty files, invalid
filenames, invalid UTF-8, unreadable files, escaping symbolic links, and exceeded limits cause an
explicit validation error before a new chat run starts; files are never silently truncated.
Non-Markdown files and nested directories are not loaded or executed.

For bundled recipes, add a file under `backend/prompting/defaults/skills/`. Existing package-data
rules include these Markdown files in installations. Keep application-wide policy in the shared
prompts, not in individual skills.

### Editing API

`GET /api/skills` lists effective bundled/local instructions; `GET /api/skills/{name}` reads
one recipe. `PUT /api/skills/{name}` accepts `content` and `expected_revision`. Use the revision
returned by a read when updating, or `null` when creating a new name. Responses contain the name,
content, source (`bundled` or `local`), and revision. Saving validates the entire resulting skill
set before writing, so an edit cannot exceed the shared count or content budget.

The backend serializes API saves, checks revisions, and atomically replaces the local file.
Stale revisions return HTTP 409 without overwriting the current file. Already-visible external
edits are detected; this is not an atomic compare-and-swap with external filesystem editors.
The TUI retains the draft on failure: copy it before reopening the skill to reconcile changes.
These management endpoints are not exposed as model tools.
