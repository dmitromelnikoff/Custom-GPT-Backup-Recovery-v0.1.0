# Custom GPT Backup & Recovery Toolkit

**A reproducible, local-first backup workflow for user-owned Custom GPTs.** Includes a field-tested browser-UI Knowledge downloader, profile-icon downloader, offline integrity/provenance auditing, Obsidian recovery, and a documented recovery case study.

**Primary documentation:** [Русская инструкция](README_RU.md) · [From-zero workflow](docs/PLAYBOOK_RU.md) · [Case study](docs/CASE_STUDY_RU.md) · [What did not work](docs/DECISIONS_RU.md) · [Architecture](docs/ARCHITECTURE_RU.md) · [Security](SECURITY.md)

> [!IMPORTANT]
> This is an **unofficial** community project, not affiliated with or endorsed by OpenAI. The editor UI and web response formats can change. Work only with GPTs and files you own/have permission to back up. Never expose authentication profiles or private GPT contents.

## What is included

| Component | Maturity | Purpose |
| --- | --- | --- |
| `scripts/download_direct_gpt_knowledge_ui_v11.py` | **Field-tested** | Download Knowledge via the visible *Configure → Knowledge* editor UI; resume after errors; stop on access restrictions. |
| `scripts/download_custom_gpt_icons_v2_safe.py` | **Field-tested (user confirmed all icons)** | Save GPT **profile icons**, not Knowledge attachments. |
| `scripts/auto_restore_knowledge_from_obsidian.py` | **Field-tested** | Read-only search of local Obsidian vault; install carefully matched candidate copies. |
| `scripts/obsidian_knowledge_recovery.py`, `scripts/install_obsidian_candidates_into_gpts.py`, `scripts/resolve_obsidian_shared_by_sha.py` | **Field-tested** | Manual review and repeated-source matching. |
| `scripts/audit_shared_gpt_knowledge.py` | **Field-tested** | Metadata-based shared-source analysis; no assumption of byte-identical originals. |
| `scripts/prepare_remaining_library_phase2.py` | **Field-tested** | Missing-file list and source-confidence review. |
| `tools/archive_cli.py` | **New; offline-tested** | One-command audit and generic import from a local folder, with candidate provenance. |
| `experimental/bootstrap_snapshot_legacy.py` | **Legacy / unstable** | Optional initial snapshot and Instructions capture using undocumented web endpoints. |
| `experimental/download_library_custom_gpt_ui_v16_guarded.py` | **Experimental / not proven** | Guarded Library diagnostics; do not treat as a source of confirmed originals. |

## Quick start (Windows PowerShell)

```powershell
python -m pip install -r requirements.txt

# Start with your private snapshot in Custom_GPT_backup/gpt_list_snapshot.json
python scripts/download_direct_gpt_knowledge_ui_v11.py --all-gpts --plan-only

# Connect to a previously authorized Chrome opened with local CDP debugging
python -u scripts/download_direct_gpt_knowledge_ui_v11.py --unattended --attach --confirm-access-restored --pause-min 15 --pause-max 30

# Offline inventory with explicit direct-SHA verification vs candidates
python tools/archive_cli.py audit

# Icons (different from files inside Knowledge)
python scripts/download_custom_gpt_icons_v2_safe.py --attach --pause-min 15 --pause-max 30
```

**No snapshot yet?** Follow the [from-zero workflow](docs/PLAYBOOK_RU.md). The historic snapshot collector is provided **only as an optional, undocumented/fragile bootstrap**; there is no guarantee it will continue to work on newer ChatGPT interfaces. The stable backup phase requires a valid local snapshot. If you already maintain a private CSV of all your own GPT/file IDs and sizes, `tools/snapshot_from_csv.py` converts it offline (see `examples/sample_inventory.csv`).

## Final reported outcome of the case study

- **250 GPTs**; **1,869** Knowledge entries in the snapshot.
- **1,865 of 1,869** Knowledge destination files present with expected file sizes (**4** Knowledge image files still missing at the last verified audit).
- **1,676** files matched direct-editor/manual-direct SHA-256 receipt records.
- **189** additional files present with matching sizes but **not independently proven to match the target GPT originals** (Library and local-archive candidates).
- The owner reported that all GPT profile icons had been downloaded; no independent icon-by-icon audit was supplied here.

All counts refer to **the user's final available logs**, not data bundled in this public repository. No private user backup files are included.

## Data never to publish

`Custom_GPT_backup/`, `gpt_list_snapshot.json`, GPT Instructions, Knowledge, Actions, logs, Chrome profiles, cookies, tokens and private archive contents. See [SECURITY.md](SECURITY.md) and `.gitignore`.

## Limits and compatibility

- Primarily field-tested on **Windows + Google Chrome + Playwright**, Russian ChatGPT editor UI, in **October 2026**.
- Browser UI may change. On unknown controls the tool should defer rather than click destructive elements. Always test one file first.
- The collector and icon helper may use **undocumented endpoints**; their continued availability is not assured.
- No bypass of HTTP 401 / 403 / 429 is attempted. Stop when access is restricted.
- Byte identity of local/archive/Library candidates with a target GPT **cannot** be established solely from filename, size or matching metadata.

## Tests

```powershell
python -m unittest discover -s tests -v
python -m compileall -q scripts tools experimental
```

Licensed under MIT; code is provided without warranty. See [LICENSE](LICENSE).