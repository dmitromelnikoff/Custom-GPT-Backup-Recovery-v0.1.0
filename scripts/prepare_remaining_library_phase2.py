#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline, read-only inventory of missing original GPT Knowledge.

Produces a validated conservative queue for the separate Library-candidate workflow.
Does NOT assume identical filenames or sizes imply original-file equality.
Does NOT copy or modify any Knowledge files.
"""
from __future__ import annotations
import argparse, collections, csv, hashlib, json, re, unicodedata
from pathlib import Path


def safe_part(s, limit=100):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        s = '_' + s
    return s


def norm(s):
    return unicodedata.normalize('NFC', str(s)).casefold().strip()


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def sha256_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_snapshot(snapshot, backup):
    data = json.loads(snapshot.read_text(encoding='utf-8-sig'))
    gpts = data.get('gpts', []) if isinstance(data, dict) else data
    rows = []
    for entry in gpts:
        r = entry.get('resource', entry)
        g = r.get('gizmo', r)
        gid = g.get('id', '')
        if not isinstance(gid, str) or not gid.startswith('g-') or gid.startswith('g-p-'):
            continue
        name = (g.get('display') or {}).get('name') or gid
        base = backup / 'gpts' / safe_part(name, 68) / safe_part(gid, 90) / 'knowledge'
        for i, obj in enumerate(r.get('files') or [], 1):
            fn, size = obj.get('name'), obj.get('size')
            if not isinstance(fn, str) or not isinstance(size, int) or not 0 < size < 512 * 1024 * 1024:
                continue
            fid = obj.get('file_id') or ''
            dest = base / f'{i:02d}_{safe_part(fn)}'
            rows.append(dict(gpt_name=name, gpt_id=gid, file_name=fn,
                             file_id=fid, expected_bytes=size, ordinal=i,
                             dest=dest, normalized_name=norm(fn)))
    return rows


def read_deferred(backup):
    p = backup / 'direct_gpt_deferred_originals.json'
    if not p.is_file():
        return {}
    data = json.loads(p.read_text(encoding='utf-8-sig'))
    return {(x.get('gpt_id'), x.get('file_id')): x.get('status', '')
            for x in data.get('items', []) if isinstance(x, dict)}


def read_direct_provenance(backup):
    receipts = {}
    p = backup / 'direct_gpt_ui_knowledge_inventory.csv'
    if p.is_file():
        with p.open('r', encoding='utf-8-sig', newline='') as f:
            for row in csv.DictReader(f, delimiter=';'):
                if row.get('status') != 'SAVED_DIRECT_GPT_EDITOR_SIZE_MATCH':
                    continue
                m = re.search(r'SHA-256\s+([a-fA-F0-9]{64})\b', row.get('note') or '')
                if m:
                    receipts[(row.get('gpt_id'), row.get('file_id'))] = ('DIRECT_EDITOR', m.group(1).lower())
    p = backup / 'direct_gpt_knowledge_import_receipts.jsonl'
    if p.is_file():
        with p.open('r', encoding='utf-8-sig') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if rec.get('result') not in {'RESTORED_FROM_DIRECT_GPT_DOWNLOAD', 'ALREADY_PRESENT_EXACT_HASH'}:
                    continue
                if rec.get('source_claim') != 'user_manual_download_from_GPT_Knowledge':
                    continue
                digest = (rec.get('original') or {}).get('sha256')
                if isinstance(digest, str) and re.fullmatch(r'[0-9a-fA-F]{64}', digest):
                    receipts[(rec.get('gpt_id'), rec.get('file_id'))] = ('MANUAL_GPT_EDITOR', digest.lower())
    return receipts


def has_valid_candidate(backup, row):
    folder = backup / 'Shared_Knowledge_candidates' / safe_part(row['gpt_id'], 90) / safe_part(row['file_id'], 90)
    if not folder.is_dir():
        return False
    for p in folder.glob('*.json'):
        try:
            obj = json.loads(p.read_text(encoding='utf-8'))
            if obj.get('status') != 'SHARED_SOURCE_CANDIDATE_NOT_TARGET_ORIGINAL':
                continue
            target = obj.get('target', {})
            if (target.get('gpt_id'), target.get('file_id')) != (row['gpt_id'], row['file_id']):
                continue
            digest = obj.get('candidate_sha256', '')
            candidate = folder / (digest + '.bin')
            if (re.fullmatch(r'[0-9a-f]{64}', digest) and candidate.is_file()
                    and candidate.stat().st_size == row['expected_bytes']
                    and sha256_file(candidate) == digest):
                return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def main():
    ap = argparse.ArgumentParser(description='Офлайн-очередь только оставшихся Knowledge для Library; без Chrome')
    ap.add_argument('--snapshot', default='Custom_GPT_backup/gpt_list_snapshot.json')
    ap.add_argument('--backup', default='Custom_GPT_backup')
    ap.add_argument('--out', default=None, help='Каталог отчётов, по умолчанию <backup>/Library_phase2_plan')
    args = ap.parse_args()
    snapshot = Path(args.snapshot).expanduser().resolve()
    backup = Path(args.backup).expanduser().resolve()
    out = Path(args.out).expanduser().resolve() if args.out else backup / 'Library_phase2_plan'
    if not snapshot.is_file():
        ap.error(f'Нет snapshot: {snapshot}')
    all_rows = parse_snapshot(snapshot, backup)
    name_ids = collections.defaultdict(set)
    exact_key_count = collections.Counter()
    for row in all_rows:
        name_ids[row['normalized_name']].add(row['file_id'])
        exact_key_count[(row['gpt_id'], row['file_id'], row['normalized_name'], row['expected_bytes'])] += 1
    deferred = read_deferred(backup)
    receipts = read_direct_provenance(backup)
    missing, safe, ambiguous, provenance_review = [], [], [], []
    direct_verified = 0
    size_match = 0
    for r in all_rows:
        dest = r['dest']
        if dest.is_file() and dest.stat().st_size == r['expected_bytes']:
            size_match += 1
            claim = receipts.get((r['gpt_id'], r['file_id']))
            if claim and sha256_file(dest) == claim[1]:
                direct_verified += 1
            else:
                provenance_review.append({k: r[k] for k in ('gpt_name', 'gpt_id', 'file_name', 'file_id', 'expected_bytes')}
                                         | {'file_path': str(dest), 'reason': 'SIZE_MATCH_ONLY_OR_UNVERIFIED_PROVENANCE'})
            continue
        collision = len(name_ids[r['normalized_name']]) != 1 or exact_key_count[(r['gpt_id'],r['file_id'],r['normalized_name'],r['expected_bytes'])] != 1
        needs = {k: r[k] for k in ('gpt_name', 'gpt_id', 'file_name', 'file_id', 'expected_bytes', 'ordinal')}
        needs['deferred_status'] = deferred.get((r['gpt_id'], r['file_id']), '')
        needs['shared_candidate_present'] = 'YES_UNVERIFIED' if has_valid_candidate(backup, r) else 'NO'
        needs['dest'] = str(dest)
        needs['phase2_priority'] = 'AMBIGUOUS_NAME_REVIEW' if collision else 'LIBRARY_SAFE_NAME_SEARCH'
        if dest.exists():
            needs['phase2_priority'] = 'LOCAL_PATH_SIZE_MISMATCH_MANUAL_REVIEW'
            collision = True
        missing.append(needs)
        if collision:
            ambiguous.append(needs)
        else:
            safe.append(needs)
    write_csv(out / 'missing_all.csv',
              ['gpt_name','gpt_id','file_name','file_id','expected_bytes','ordinal','deferred_status','shared_candidate_present','dest','phase2_priority'], missing)
    write_csv(out / 'library_queue_missing_unique.csv',
              ['priority','gpt_name','gpt_id','file_name','file_id','expected_bytes','library_lookup','local_path'],
              [dict(priority='MISSING_ONLY_UNIQUE_SOURCE_FILE_ID', **{k:r[k] for k in ('gpt_name','gpt_id','file_name','file_id','expected_bytes')},
                    library_lookup='PENDING', local_path=r['dest']) for r in safe])
    write_csv(out / 'ambiguous_review.csv',
              ['gpt_name','gpt_id','file_name','file_id','expected_bytes','ordinal','deferred_status','shared_candidate_present','dest','phase2_priority'], ambiguous)
    write_csv(out / 'size_match_provenance_review.csv',
              ['gpt_name','gpt_id','file_name','file_id','expected_bytes','file_path','reason'], provenance_review)
    report = dict(total_knowledge=len(all_rows), size_matched=size_match, direct_receipt_hash_verified=direct_verified,
                  size_only_or_unverified=len(provenance_review), missing=len(missing),
                  unique_eligible_library=len(safe), ambiguous_or_mismatch=len(ambiguous),
                  missing_deferred=sum(bool(x['deferred_status']) for x in missing),
                  shared_source_candidates=sum(x['shared_candidate_present']=='YES_UNVERIFIED' for x in missing),
                  network_calls=0, canonical_knowledge_modified=False,
                  warning='Library filename/size is not proof of original GPT content. Library downloads must remain candidate-only.')
    (out/'summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('=== ЭТАП 2. ОФЛАЙН-СВЕРКА KNOWLEDGE ===')
    for k,v in report.items():
        if k not in ('warning','network_calls','canonical_knowledge_modified'):
            print(f'{k}: {v}')
    print('ОЧЕРЕДЬ Library с однозначной идентификацией имени:', out/'library_queue_missing_unique.csv')
    print('Оставшиеся файлы:', out/'missing_all.csv')
    print('Неоднозначные/конфликтующие:', out/'ambiguous_review.csv')
    print('Сохранённые по размеру, без проверенного прямого происхождения:', out/'size_match_provenance_review.csv')
    print('Никакие файлы Knowledge и реестры исходного загрузчика не изменены.')


if __name__=='__main__':
    main()
