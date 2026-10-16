# Mutation evidence for #81 (AW#71)

Both patches mutate the interpreter `WorkspaceChildJournalReader.page` resolves
(`agentic_isolation/child_journal.py`). Killing tests:
`test_real_export_roundtrip_recovers_late_binding` and
`test_native_lifecycle_and_conflict_roundtrip` in
`lib/python/agentic_isolation/tests/test_child_journal.py`.

- `01-pin-host-python3-path.patch`: a fixed host path instead of the image's
  `python3`. Killed when that path is absent or lacks agentic_session_store.
- `02-resolve-python-not-python3.patch`: a different bare name. Killed only
  because the fixture resolves exactly `python3` through its own PATH, not
  whatever the invoking shell has.

Deleted before merge; the mutation-evidence PR comment is the record.
