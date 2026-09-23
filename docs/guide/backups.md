# Backup and recovery

Snapshots stay under the current workspace's `snapshots/` directory. They
contain plaintext memory; SHA-256 checksums detect accidental changes, not
malicious rewriting of both files and manifest. Use encrypted workspace exports
when moving private memory to storage you do not control.

```bash
pmb snapshot create --note "before cleanup"
pmb snapshot list
pmb snapshot list --json
pmb snapshot verify 20260923-120000-123456
```

Creation uses SQLite's online backup API for workspace `.sqlite` files,
including cold storage. Committed WAL data is included without copying live
WAL/SHM files. Other workspace files are copied, then every file is hashed and
SQLite integrity is checked. Only a fully verified snapshot appears in the
list; partial staging directories are removed on ordinary failures.

SQLite is transactionally consistent. The entire workspace is not a single
transaction across SQLite, LanceDB, and configuration files. Stop writers before
creation when you need those files captured at exactly the same point in time.
Snapshots reject symlinks rather than follow files outside the workspace.

## Restore

Stop the daemon, close agent connections, and stop dashboards or direct writers:

```bash
pmb daemon stop
pmb snapshot verify <id>
pmb snapshot restore <id>
```

Restore refuses while registered PMB servers are alive. The registry cannot
identify every direct Python/CLI writer, so stopping those remains necessary.
`--yes` skips the prompt; it does not bypass validation or the server guard.

Restore validates the source, stages and rechecks its files, and creates a
verified `pre-restore-*` snapshot. It replaces the workspace contents, removing
files absent from the chosen snapshot. Existing snapshots stay available. On an
ordinary file-operation failure the moves are rolled back; the safety snapshot
also remains. Restart PMB/agents after recovery so in-memory indexes are reopened.

A process kill or power loss during the multi-file restore is not atomic. Stop
writers and restore from the retained `pre-restore-*` snapshot after restarting.
Snapshots made before 1.3 can be checked for workspace/SQLite validity, but have
no file checksums; the CLI labels them as legacy. An unreadable manifest, foreign
workspace, unsupported format, checksum mismatch, or failed SQLite integrity
check prevents restoration.
