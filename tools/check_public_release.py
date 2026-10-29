#!/usr/bin/env python3
"""Conservative heuristic scan of public source tree. NOT a substitute for review."""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

BLOCKED_PATH_PARTS = {
    'Custom_GPT_backup', '.chatgpt_backup_normal_chrome_profile',
    '.chatgpt_backup_chrome_profile', 'Direct_GPT_UI_probes',
    'My Downloads Arhiv', 'My Downloads Arhiv final', 'Shared_Knowledge_candidates',
}
BLOCKED_NAMES = {'gpt_list_snapshot.json', 'export_manifest.json', 'direct_gpt_ui_knowledge_inventory.csv'}
ID_PATTERN = re.compile(r'(?<![a-zA-Z0-9])g-[a-f0-9]{28,36}(?![a-zA-Z0-9])')
FILE_PATTERN = re.compile(r'file_0{6,}[a-f0-9]{14,}')


def scan(root: Path):
    issues = []
    for p in root.rglob('*'):
        rel=p.relative_to(root)
        if '__pycache__' in rel.parts or '.git' in rel.parts or '.venv' in rel.parts:
            continue
        if not p.is_file(): continue
        if any(part in BLOCKED_PATH_PARTS for part in rel.parts) or p.name in BLOCKED_NAMES:
            issues.append((str(rel),'private path/name'))
            continue
        if p.suffix in {'.jsonl', '.har', '.log', '.png', '.jpg', '.jpeg', '.xlsx', '.zip'}:
            issues.append((str(rel),'unexpected potentially private artifact'))
            continue
        if p.suffix.lower() in {'.py','.md','.txt','.json','.csv','.ps1','.yml','.yaml'}:
            text=p.read_text(encoding='utf-8-sig',errors='replace')
            if ID_PATTERN.search(text) or FILE_PATTERN.search(text):
                issues.append((str(rel),'looks like a real account GPT/file ID'))
    return issues


def main(argv=None):
    ap=argparse.ArgumentParser(description='Heuristic pre-publication privacy scan; manual review is STILL needed')
    ap.add_argument('--root', default=str(Path(__file__).resolve().parents[1]))
    args=ap.parse_args(argv)
    root=Path(args.root).resolve()
    issues=scan(root)
    for p,reason in issues:print('WARNING:',p,'-',reason)
    if issues:
        print('Refuse publication until these are checked/removed. Issues:',len(issues))
        return 1
    print('No obvious private artifacts detected. MANUALLY REVIEW git ls-files and all content before publishing.')
    return 0

if __name__ == '__main__':sys.exit(main())
