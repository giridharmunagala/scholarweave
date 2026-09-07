# Research Workspace Layout Upgrade

This is the first implementation increment of the personal research workspace. It changes actual
save locations and removes runtime dependence on legacy paper paths and JSON metadata indexes.
Projects, experiment imports and semantic visual PDF reading are subsequent increments, not included
in this change.

## New Layout

```text
workspace/
  library/papers/<readable-title--stable-id>/
    source.pdf
    notes.md
    summary.md
    summaries/
    evidence/
  knowledge/<readable-title--stable-id>.md
  knowledge/imported/<previous-relative-note-path>
```

Paper IDs and note IDs remain stable. Renaming a paper changes its display name, not its folder.
Source filenames remain in document metadata. Original PDF bytes, source revisions, note contents,
summary versions, evidence and indexed tags are preserved. Unassigned legacy notes are imported
without inventing project membership. The upgrade never sends research data to a model.

Old conversation and agent-run rows are intentionally retired. A database backup retains them for
offline inspection or restoring the old application; the new runtime does not translate their paths.
Generated extraction files remain in `local_data/artifacts/`.

## Preview And Apply

Stop the backend and other applications that write the workspace before applying or restoring.
Do not run two upgrade processes. `--offline` is an explicit operator confirmation, not a mechanism
for stopping other processes.

From the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m scripts.upgrade_workspace
.\.venv\Scripts\python.exe -m scripts.upgrade_workspace --apply --offline
```

Preview is read-only. It reports source/destination paths, bytes to copy, the backup location and
conversation retirement. Destination collisions, unsupported schema generations and changed source
PDF hashes are rejected. Symbolic links and junctions are not followed. Resolve reported conflicts
before applying; the tool does not merge or overwrite colliding research files.

Custom installations can supply `--data-dir` and `--workspace-dir` to both commands. Use the same
configuration as the backend. The current database schema must already be supported by this version.

Apply creates `local_data/workspace-layout-backup/` containing the original SQLite database,
workspace, legacy source-document tree, checksums and upgrade journal. Allow disk space for that
backup plus the relocated copies. Copy failures leave originals intact. Re-run the same apply
command after a copy/metadata/cleanup interruption; verified copies are reused. Startup refuses an
unfinished upgrade or an old managed layout.

The backup is retained after success. Application links to summary versions resolve their actual
current locations; immutable historical JSON and Markdown contents are not rewritten. Links written
manually using old absolute/root-relative paths may need updating. The journal's move list supplies
the exact mapping; relative links within moved paper folders remain valid.

## Restore

With all writers stopped:

```powershell
.\.venv\Scripts\python.exe -m scripts.upgrade_workspace --restore --offline
```

Restore verifies backup checksums first and restores the matched original database and directories.
Current directories are retained beside them with `.before-layout-restore` suffixes; the current
database is retained as `before-restore.sqlite3` inside the backup. This preserves research created
after the upgrade. Keep those copies until recovery is verified.

Restoring intentionally returns to the old layout, which requires the old application version.
Archive the restored upgrade backup before planning another upgrade. If restore itself is interrupted,
the application refuses startup and the command stops rather than overwriting either directory copy;
complete recovery offline using the matched backups and retained directories.