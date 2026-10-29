# Contributing

Bug reports and patches are welcome. Please use **synthetic** GPT IDs, file names and file bodies in tests and issues. Do not share or upload real GPT Knowledge, Instructions, saved browser profiles or credentials.

PRs should preserve these invariants:

1. Never modify a GPT or click destructive editor controls.
2. Never write to the user's source Obsidian/archive vault.
3. Do not silently overwrite existing backup files.
4. Maintain explicit provenance (`GPT ID`, `file ID`, `SHA-256`, source type) and avoid promoting candidates to originals by name or size alone.
5. Stop rather than circumvent HTTP 401/403/429.
6. Add offline tests and update the Russian playbook if a user-facing CLI changes.

Run `python -m unittest discover -s tests -v` before submitting changes.
