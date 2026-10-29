# Security policy

## Extremely important: this repository is code only

Never commit or share:

- Authenticated Chrome profile folders (cookies, sessions, tokens), Playwright traces, CDP access links.
- `gpt_list_snapshot.json` from a real account, GPT Instructions, Knowledge files, Actions configurations, or backup inventory/provenance journals.
- GPT screenshots, diagnostics with private names, signed URLs, or archives containing personal data.
- The real `Custom_GPT_backup/` tree, `My Downloads Arhiv*`, Obsidian vault exports, or browser local storage.

`.gitignore` is defense-in-depth, **not a guarantee**. Inspect `git status --short`, `git ls-files`, release ZIPs and the git history before publishing.

## Browser / endpoints

- Chrome remote debugging can expose an authenticated browsing session. Use a **dedicated Chrome profile**, bind CDP to **127.0.0.1 only** and close it when done. Never expose the CDP port on LAN/public internet.
- The `experimental/bootstrap_snapshot_legacy.py` and icon helper touch undocumented ChatGPT web interfaces; they are not officially supported APIs. The icon helper retrieves an access token transiently in memory; it must never write it to logs.
- Use on your own data only. The project does not bypass access restrictions. On HTTP **401/403/429** stop and review the cause; do not attempt evasion.
- Use the **visible Configure → Knowledge UI** as the default Knowledge source; on missing elements refuse unknown clicks rather than modifying GPT settings.

## Filesystem / provenance

- Source Obsidian/archive trees must be **read-only inputs**: no renaming, deletion or moving.
- Candidate imports are explicitly **not** cryptographically verified server originals. Preserve file IDs, sizes, hashes and source paths in private receipts.
- Do not overwrite previously downloaded Knowledge or profile icons without a separate explicit comparison and backup.
- Avoid running this program with administrator privileges.

## Reporting a security issue

Do not attach credentials, private GPT content or your full browser profile to an issue. Provide sanitized logs and a minimal synthetic reproducer.
