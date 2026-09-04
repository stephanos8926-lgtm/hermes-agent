# Lint + Dead Code

- py_compile: 4/4 OK (process_registry, base, terminal_tool, config_defaults)
- ruff check not run (no ruff in venv this image) -- manual: no unused imports found in diffed files.
- Dead code: phase0-recon/*.log untracked, feature/rw-live-backup-20260824 stale branch. No dead code in new paths beyond existing TODOs.
- Shell locale warning en_US.UTF-8 missing -- pre-existing, unrelated.
