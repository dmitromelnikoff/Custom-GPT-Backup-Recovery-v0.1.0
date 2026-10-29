#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Безопасно размещает уже staged Obsidian-кандидаты в папки соответствующих GPT.

    python install_obsidian_candidates_into_gpts.py           # Только проверка
    python install_obsidian_candidates_into_gpts.py --commit  # Фактическое копирование

Идентификация: GPT ID + Knowledge file ID из snapshot. Проверки SHA-256 и размера.
НЕ подтверждает идентичность файла из Obsidian оригиналу из Knowledge ChatGPT.
НЕ перезаписывает существующие Knowledge. Никаких сетевых запросов.
"""
from __future__ import annotations
import argparse
import collections
import csv
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 512 * 1024 * 1024
META_STATUS = 'OBSIDIAN_ARCHIVE_CANDIDATE_NOT_GPT_ORIGINAL'
SOURCE_CLAIM = 'FILE_FROM_USER_OBSIDIAN_OR_LOCAL_ARCHIVE_UNVERIFIED'
RECEIPT_STATUS = 'INSTALLED_FROM_OBSIDIAN_ARCHIVE_UNVERIFIED'
REPORT_FIELDS = ('GPT', 'GPT_ID', 'Knowledge', 'File_ID', 'Ожидаемый_размер',
                 'SHA256', 'Кандидат', 'Папка_Knowledge', 'Статус', 'Комментарий')


def safe_part(s, limit=100):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        s = '_' + s
    return s


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def load_targets(snapshot_path, backup):
    raw = json.loads(snapshot_path.read_text(encoding='utf-8-sig'))
    gpts = raw.get('gpts', []) if isinstance(raw, dict) else raw
    targets = {}
    conflicted = set()
    for item in gpts:
        r = item.get('resource', item)
        g = r.get('gizmo', r)
        gid = g.get('id', '')
        if not isinstance(gid, str) or not gid.startswith('g-') or gid.startswith('g-p-'):
            continue
        name = (g.get('display') or {}).get('name') or gid
        base = backup / 'gpts' / safe_part(name, 68) / safe_part(gid, 90) / 'knowledge'
        for idx, obj in enumerate(r.get('files') or [], 1):
            fn, size, fid = obj.get('name'), obj.get('size'), obj.get('file_id')
            if not (isinstance(fn, str) and isinstance(size, int)
                    and 0 < size <= MAX_BYTES and isinstance(fid, str) and fid):
                continue
            key = (gid, fid)
            target = dict(gpt=name, gid=gid, fid=fid, filename=fn, size=size,
                          path=base / f'{idx:02d}_{safe_part(fn)}')
            if key in targets and targets[key]['path'] != target['path']:
                conflicted.add(key)
            targets[key] = target
    return targets, conflicted


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS, delimiter=';', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def outcome(target, status, description, source=None, digest=''):
    return {
        'GPT': target.get('gpt', ''),
        'GPT_ID': target.get('gid', ''),
        'Knowledge': target.get('filename', ''),
        'File_ID': target.get('fid', ''),
        'Ожидаемый_размер': target.get('size', ''),
        'SHA256': digest,
        'Кандидат': str(source) if source else '',
        'Папка_Knowledge': str(target.get('path', '')),
        'Статус': status,
        'Комментарий': description,
    }


def validate_meta(meta_path, base, targets, conflicted):
    try:
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
        target_metadata = meta.get('target') or {}
        gid, fid = target_metadata.get('gpt_id'), target_metadata.get('file_id')
        if not (isinstance(gid, str) and isinstance(fid, str)):
            return None, None, '', 'INVALID_METADATA', 'Нет GPT ID или File ID в метаданных.'
        key = (gid, fid)
        t = targets.get(key)
        if t is None:
            return {'gpt':target_metadata.get('gpt_name', ''), 'gid':gid,'fid':fid,
                    'filename':target_metadata.get('file_name', '')}, None, '', 'NOT_IN_CURRENT_SNAPSHOT', 'Исходная запись отсутствует в snapshot.'
        if key in conflicted:
            return t, None, '', 'DUPLICATE_FILE_ID_IN_SNAPSHOT', 'Одинаковый GPT ID + File ID имеет разные пути.'
        digest = meta.get('candidate_sha256')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            return t, None, '', 'INVALID_METADATA', 'Неверный SHA-256 в метаданных.'
        source = meta_path.with_name(f'{digest}.bin')
        if meta_path.name != f'{digest}.json':
            return t, source, digest, 'INVALID_METADATA', 'Имя JSON не соответствует SHA-256.'
        if meta_path.parent != base / safe_part(gid, 90) / safe_part(fid, 90):
            return t, source, digest, 'INVALID_METADATA', 'Папка кандидата не соответствует GPT ID + File ID.'
        if (meta.get('status') != META_STATUS or meta.get('source_claim') != SOURCE_CLAIM):
            return t, source, digest, 'UNTRUSTED_PROVENANCE', 'Кандидат не является проверенным результатом Obsidian stage.'
        if (target_metadata.get('file_name') != t['filename'] or
            target_metadata.get('expected_bytes') != t['size'] or
            meta.get('candidate_bytes') != t['size']):
            return t, source, digest, 'METADATA_MISMATCH', 'Имя файла или размер в JSON отличается от snapshot.'
        if not source.is_file() or source.is_symlink():
            return t, source, digest, 'CANDIDATE_MISSING', 'Нет .bin кандидата.'
        if source.stat().st_size != t['size']:
            return t, source, digest, 'WRONG_SIZE', 'Размер кандидата отличается от snapshot.'
        if sha256(source) != digest:
            return t, source, digest, 'WRONG_SHA256', 'Содержимое .bin не соответствует SHA-256.'
        return t, source, digest, 'VALID_CANDIDATE', ''
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return {}, None, '', 'INVALID_METADATA', str(exc)


def install_one(t, src, digest, commit, receipts):
    dest = t['path']
    if dest.exists():
        if not dest.is_file():
            return outcome(t, 'BLOCKED_DESTINATION_EXISTS', 'Путь занят не файлом.', src, digest)
        if dest.stat().st_size == t['size'] and sha256(dest) == digest:
            return outcome(t, 'ALREADY_PRESENT_IDENTICAL', 'Файл в папке GPT уже побайтово совпадает; без изменений.', src, digest)
        return outcome(t, 'BLOCKED_DESTINATION_EXISTS', 'Там уже другой файл: перезапись ЗАПРЕЩЕНА.', src, digest)
    if not commit:
        return outcome(t, 'READY_TO_INSTALL', 'Можно скопировать; требуется --commit.', src, digest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with src.open('rb') as fsrc, dest.open('xb') as fdst:
            created = True
            shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
        if dest.stat().st_size != t['size'] or sha256(dest) != digest:
            dest.unlink(missing_ok=True)
            return outcome(t, 'COPY_VERIFICATION_FAILED', 'Созданный файл удалён: не прошла проверка размера или SHA.', src, digest)
        receipt = {
            'status': RECEIPT_STATUS,
            'is_direct_gpt_original': False,
            'source_claim': SOURCE_CLAIM,
            'gpt_name': t['gpt'], 'gpt_id':t['gid'], 'file_id':t['fid'],
            'file_name':t['filename'], 'expected_bytes':t['size'],
            'sha256':digest, 'candidate_path':str(src), 'installed_path':str(dest),
            'installed_utc':datetime.now(timezone.utc).isoformat(),
            'warning':'Копия из Obsidian помещена в Knowledge GPT, но точная идентичность исходному экземпляру Knowledge не доказана.'
        }
        try:
            with receipts.open('a', encoding='utf-8') as f:
                f.write(json.dumps(receipt, ensure_ascii=False) + '\n')
        except OSError as exc:
            return outcome(t, 'INSTALLED_BUT_RECEIPT_ERROR',
                           f'Файл УЖЕ скопирован, но журнал не записан: {exc}', src, digest)
        return outcome(t, RECEIPT_STATUS, 'Скопировано в соответствующий GPT. Происхождение: Obsidian, НЕ подтверждённый оригинал.', src, digest)
    except (OSError, ValueError) as exc:
        # Удаляем только недописанный файл, созданный нами; чужой не затрагиваем.
        if created:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
        return outcome(t, 'COPY_ERROR', str(exc), src, digest)


def run(args):
    backup = Path(args.backup).expanduser().resolve()
    snapshot = Path(args.snapshot).expanduser().resolve() if args.snapshot else backup / 'gpt_list_snapshot.json'
    if not snapshot.is_file():
        raise SystemExit(f'Отсутствует snapshot: {snapshot}')
    candidates = backup / 'Obsidian_Archive_candidates'
    if not candidates.is_dir():
        raise SystemExit(f'Нет папки кандидатов: {candidates}\nСначала: python obsidian_knowledge_recovery.py stage')
    targets, conflicted = load_targets(snapshot, backup)
    metas = sorted(candidates.glob('*/*/*.json'))
    groups = collections.defaultdict(list)
    errors = []
    for p in metas:
        t, source, digest, status, reason = validate_meta(p, candidates, targets, conflicted)
        if status != 'VALID_CANDIDATE':
            errors.append(outcome(t or {}, status, reason, source, digest))
        else:
            groups[(t['gid'], t['fid'])].append((t, source, digest))
    results = list(errors)
    receipts = backup / 'obsidian_knowledge_installed_receipts.jsonl'
    for key, options in sorted(groups.items()):
        uniq = {x[2] for x in options}
        if len(uniq) > 1:
            t, s, d = options[0]
            results.append(outcome(t, 'BLOCKED_MULTIPLE_VERSIONS',
                                   f'Для одного Knowledge найдено {len(uniq)} разных SHA-256; нужно выбрать версию вручную.', s, d))
            continue
        t, s, d = options[0]
        results.append(install_one(t, s, d, args.commit, receipts))
    report_dir = backup / 'Obsidian_Knowledge_recovery'
    report = report_dir / '08_УСТАНОВКА_В_КАТАЛОГИ_GPT.csv'
    write_csv(report, results)
    counts = collections.Counter(x['Статус'] for x in results)
    print('=== OBSIDIAN → ПАПКИ КОНКРЕТНЫХ GPT ===', flush=True)
    print('Режим:', 'КОПИРОВАНИЕ --commit' if args.commit else 'ПРОВЕРКА БЕЗ ИЗМЕНЕНИЙ', flush=True)
    print('GPT в snapshot:', len({x[0] for x in targets}), '; записей Knowledge:', len(targets), flush=True)
    print('Файлов-кандидатов:', len(metas), '; целевых Knowledge:', len(groups), flush=True)
    for status, qty in sorted(counts.items()):
        print(f'  {status}: {qty}', flush=True)
    print('Отчёт:', report, flush=True)
    if args.commit:
        print('Отдельный журнал происхождения:', receipts, flush=True)
    print('Оригинальные файлы не перезаписывались. Копии из Obsidian помечены как неподтверждённые.', flush=True)
    return 0 if not any(x['Статус'] in {'COPY_ERROR','COPY_VERIFICATION_FAILED'} for x in results) else 1


def main():
    ap = argparse.ArgumentParser(description='Копирование staged Obsidian Knowledge в каталоги конкретных GPT с сохранением происхождения.')
    ap.add_argument('--commit', action='store_true', help='Выполнить копирование (иначе только отчёт)')
    ap.add_argument('--backup', default='Custom_GPT_backup', help='Папка backup')
    ap.add_argument('--snapshot', help='Путь к gpt_list_snapshot.json; по умолчанию внутри --backup')
    sys.exit(run(ap.parse_args()))

if __name__ == '__main__':
    main()
