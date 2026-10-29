#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline archive audit and candidate import. No browser or network required.

Authoritative direct-download receipt verification uses the tested v11 logic.
Manual/local imports are explicitly NON-authoritative candidate copies.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from download_direct_gpt_knowledge_ui_v11 import (  # noqa: E402
    all_gpt_ids, build_shared_index, parse_gpt, safe_part,
    sha256_file, trusted_direct_donors,
)


def normalized(s):
    return unicodedata.normalize('NFC', str(s)).casefold()


def read_targets(snapshot: Path, backup: Path):
    blob = json.loads(snapshot.read_text(encoding='utf-8-sig'))
    targets = []
    for ident in all_gpt_ids(blob):
        _, rows = parse_gpt(snapshot, backup, ident, blob=blob)
        targets.extend(rows)
    return blob, targets


def dump_csv(path: Path, rows: list[dict], cols: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=cols, delimiter=';', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def atomic_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(path.name + '.tmp')
    staged.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    staged.replace(path)


def audit(snapshot: Path, backup: Path, report: Path):
    blob, targets = read_targets(snapshot, backup)
    index, _ = build_shared_index(blob, backup)
    trusted = trusted_direct_donors(backup, index)
    lines, counts = [], Counter()
    for t in targets:
        key = t['gpt_id'] + '::' + t['file_id']
        dest = t['dest']
        got = dest.stat().st_size if dest.is_file() else None
        if got is None:
            label = 'MISSING'
        elif got != t['expected']:
            label = 'WRONG_SIZE'
        elif key in trusted:
            label = 'DIRECT_HASH_VERIFIED'
        else:
            label = 'PRESENT_UNVERIFIED_OR_ARCHIVE_CANDIDATE'
        counts[label] += 1
        lines.append({
            'gpt_name': t['gpt_name'], 'gpt_id': t['gpt_id'],
            'file_name': t['name'], 'file_id': t['file_id'],
            'expected_bytes': t['expected'], 'actual_bytes': got if got is not None else '',
            'status': label, 'sha256_direct_verified': trusted[key]['sha256'] if key in trusted else '',
            'destination': str(dest),
        })
    icons = 0
    for ident in all_gpt_ids(blob):
        name, _ = parse_gpt(snapshot, backup, ident, blob=blob)
        folder = backup / 'gpts' / safe_part(name, 68) / safe_part(ident, 90)
        if any(f.is_file() for f in folder.glob('icon.*')):
            icons += 1
    dump_csv(report / 'knowledge_audit.csv', lines,
        ['gpt_name', 'gpt_id', 'file_name', 'file_id', 'expected_bytes',
         'actual_bytes', 'status', 'sha256_direct_verified', 'destination'])
    summary = {
        'gpts_in_snapshot': len(all_gpt_ids(blob)), 'knowledge_total': len(targets),
        'direct_receipt_hash_verified': counts['DIRECT_HASH_VERIFIED'],
        'present_unverified_or_archive_candidate': counts['PRESENT_UNVERIFIED_OR_ARCHIVE_CANDIDATE'],
        'missing': counts['MISSING'], 'wrong_size': counts['WRONG_SIZE'],
        'size_matched': counts['DIRECT_HASH_VERIFIED'] + counts['PRESENT_UNVERIFIED_OR_ARCHIVE_CANDIDATE'],
        'gpt_folders_with_icon': icons,
        'warning': 'Size matching is not proof of original content. Local imports remain candidates.',
    }
    atomic_json(report / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('Reports:', report)
    return 0


def exclusive_copy(src: Path, dest: Path, expected_sha: str):
    """Never overwrite: an exclusive create with copy+hash verification."""
    if dest.is_symlink():
        return 'DEST_SYMLINK_REFUSED'
    if dest.exists():
        if dest.is_file() and sha256_file(dest) == expected_sha:
            return 'ALREADY_IDENTICAL'
        return 'DEST_CONFLICT'
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with src.open('rb') as input_file, dest.open('xb') as output_file:
            while True:
                block = input_file.read(1024 * 1024)
                if not block:
                    break
                output_file.write(block)
            output_file.flush()
            os.fsync(output_file.fileno())
        if sha256_file(dest) != expected_sha:
            dest.unlink(missing_ok=True)
            return 'COPY_HASH_MISMATCH'
        return 'COPIED'
    except FileExistsError:
        return 'DEST_EXISTS'
    except BaseException:
        # Avoid leaving an incomplete newly written file after interruption.
        if dest.exists() and dest.is_file() and sha256_file(dest) != expected_sha:
            try: dest.unlink()
            except OSError: pass
        raise


def import_folder(snapshot: Path, backup: Path, source: Path, report: Path,
                  commit: bool, shared: bool):
    source = source.resolve(strict=True)
    if not source.is_dir():
        raise ValueError('Source must be a directory')
    _, targets = read_targets(snapshot, backup)
    missing = [t for t in targets if not t['dest'].is_file() or
               t['dest'].stat().st_size != t['expected']]
    wanted = defaultdict(list)
    for t in missing:
        wanted[(normalized(t['name']), t['expected'])].append(t)
    found = defaultdict(list)
    examined = 0
    for p in source.rglob('*'):
        if not p.is_file() or p.is_symlink():
            continue
        try:
            k = normalized(p.name), p.stat().st_size
        except OSError:
            continue
        examined += 1
        if k in wanted:
            found[k].append((p, sha256_file(p)))
    rows = []
    installs = 0
    inbox = backup / 'My Downloads Arhiv' / 'imported_by_archive_cli'
    receipts = backup / 'manual_candidate_import_receipts.jsonl'
    for key, group in wanted.items():
        sources = found.get(key, [])
        hashes = {h for _, h in sources}
        for t in group:
            status, chosen_sha, src_name = '', '', ''
            if not sources:
                status = 'NOT_FOUND'
            elif len(hashes) > 1:
                status = 'CONFLICTING_SOURCE_HASHES'
            elif len(group) > 1 and not shared:
                status = 'MULTIPLE_TARGETS_REQUIRES_ALLOW_SHARED_TARGETS'
            else:
                path, chosen_sha = sorted(sources, key=lambda e: str(e[0]))[0]
                src_name = str(path)
                if not commit:
                    status = 'READY_CANDIDATE'
                else:
                    # Source directory is read-only. Never write into it through links/junctions.
                    resolved_dest = t['dest'].resolve(strict=False)
                    resolved_inbox = inbox.resolve(strict=False)
                    if resolved_dest.is_relative_to(source) or resolved_inbox.is_relative_to(source):
                        status = 'DESTINATION_INSIDE_SOURCE_REFUSED'
                    else:
                        staged = inbox / (chosen_sha + '_' + safe_part(path.name, 140))
                        stage_state = exclusive_copy(path, staged, chosen_sha)
                        if stage_state not in ('COPIED', 'ALREADY_IDENTICAL'):
                            status = 'STAGE_' + stage_state
                        else:
                            status = exclusive_copy(staged, t['dest'], chosen_sha)
                            if status == 'COPIED':
                                installs += 1
                                receipts.parent.mkdir(parents=True, exist_ok=True)
                                receipt = {'status': 'LOCAL_ARCHIVE_CANDIDATE',
                                    'is_direct_gpt_original': False, 'source_file': src_name,
                                    'staged_file': str(staged), 'gpt_id': t['gpt_id'],
                                    'file_id': t['file_id'], 'file_name': t['name'],
                                    'expected_bytes': t['expected'], 'sha256': chosen_sha,
                                    'installed_path': str(t['dest']),
                                    'timestamp_utc': datetime.now(timezone.utc).isoformat()}
                                with receipts.open('a', encoding='utf-8') as f:
                                    f.write(json.dumps(receipt, ensure_ascii=False) + '\n')
            rows.append({'gpt_name': t['gpt_name'], 'gpt_id': t['gpt_id'],
                'file_name': t['name'], 'file_id': t['file_id'],
                'expected_bytes': t['expected'], 'source_path': src_name,
                'source_sha256': chosen_sha, 'candidate_count': len(sources),
                'status': status, 'destination': str(t['dest'])})
    report.mkdir(parents=True, exist_ok=True)
    dump_csv(report / 'manual_import_inventory.csv', rows,
             ['gpt_name','gpt_id','file_name','file_id','expected_bytes',
              'source_path','source_sha256','candidate_count','status','destination'])
    summary = {'missing_before':len(missing),'source_files_scanned':examined,
               'installed_candidates':installs,'status_counts':dict(Counter(r['status'] for r in rows)),
               'source_read_only':True,'original_direct_provenance':False}
    atomic_json(report / 'manual_import_summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('Reports:', report)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description='Offline GPT Knowledge archive audit / local candidate import')
    p.add_argument('--backup', default='Custom_GPT_backup')
    p.add_argument('--snapshot', default=None)
    p.add_argument('--report', default=None)
    subs = p.add_subparsers(dest='action', required=True)
    subs.add_parser('audit', help='Verify direct receipts SHA-256, size, missing and icons')
    imp = subs.add_parser('import-folder', help='Read-only scan of external folder, stage and install candidates')
    imp.add_argument('--source', required=True)
    imp.add_argument('--commit', action='store_true')
    imp.add_argument('--allow-shared-targets', action='store_true',
                     help='Place one source blob into multiple GPTs with same name/size; DOES NOT prove target originality')
    args = p.parse_args(argv)
    backup = Path(args.backup).expanduser().resolve()
    snapshot = Path(args.snapshot).expanduser().resolve() if args.snapshot else backup / 'gpt_list_snapshot.json'
    if not snapshot.is_file(): p.error('No snapshot: ' + str(snapshot))
    report = Path(args.report).expanduser().resolve() if args.report else backup / 'reports' / 'offline_audit'
    if args.action == 'audit':
        return audit(snapshot, backup, report)
    return import_folder(snapshot, backup, Path(args.source), report,
                         args.commit, args.allow_shared_targets)


if __name__ == '__main__':
    sys.exit(main())
