#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only Obsidian vault scan -> My Downloads Arhiv -> GPT Knowledge.

The Obsidian vault is NEVER written to. All creations are in Custom_GPT_backup.

  python auto_restore_knowledge_from_obsidian.py --dry-run
  python auto_restore_knowledge_from_obsidian.py --commit
  python auto_restore_knowledge_from_obsidian.py --vault "D:\\My Obsidian Vault" --commit

A filename + byte size match is a candidate, NOT proof of the original GPT bytes.
Ambiguous mappings are NEVER installed without an exact GPT-folder clue or user
approval in the generated CSV. Existing GPT files are NEVER overwritten.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_VAULT = r'X:\Vault\GPT_Assistents'
DEFAULT_BACKUP = 'Custom_GPT_backup'
MAX_BYTES = 512 * 1024 * 1024
RECEIPT_STATUS = 'INSTALLED_FROM_OBSIDIAN_ARCHIVE_UNVERIFIED'
SOURCE_CLAIM = 'FILE_FROM_USER_OBSIDIAN_OR_LOCAL_ARCHIVE_UNVERIFIED'
REPORT_FIELDS = [
    'GPT', 'GPT_ID', 'Knowledge', 'File_ID', 'Размер_ожидаемый',
    'Найдено_вариантов', 'Источник_Obsidian', 'Inbox_копия',
    'Папка_Knowledge', 'SHA256', 'Правило_выбора', 'Статус', 'Комментарий',
]
FOUND_FIELDS = ['Файл_Obsidian', 'Имя', 'Размер', 'SHA256', 'Inbox_копия', 'Статус', 'Комментарий']
REVIEW_FIELDS = ['GPT', 'GPT_ID', 'Knowledge', 'File_ID', 'Размер_ожидаемый',
                 'Файл_Obsidian', 'SHA256', 'Причина', 'Подтвердить_ДА']
EXCLUDED_DIRS = {'.obsidian', '.git', '.trash', '.stfolder', '.svn'}


def say(*args):
    print(*args, flush=True)


def norm(text):
    return unicodedata.normalize('NFC', str(text)).casefold().strip()


def safe_part(s, limit=100):
    # Matches the existing GPT backup layout exactly.
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        s = '_' + s
    return s


def is_reparse(p):
    try:
        info = p.lstat()
        return bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if hasattr(info, 'st_file_attributes') else p.is_symlink()
    except OSError:
        return True


def sha256(p):
    h = hashlib.sha256()
    with p.open('rb') as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields, delimiter=';', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def load_snapshot(snapshot_path, backup):
    data = json.loads(snapshot_path.read_text(encoding='utf-8-sig'))
    gpts = data.get('gpts', []) if isinstance(data, dict) else data
    result = []
    for item in gpts:
        r = item.get('resource', item)
        g = r.get('gizmo', r)
        gid = g.get('id', '')
        if not isinstance(gid, str) or not gid.startswith('g-') or gid.startswith('g-p-'):
            continue
        name = (g.get('display') or {}).get('name') or gid
        root = backup / 'gpts' / safe_part(name, 68) / safe_part(gid, 90) / 'knowledge'
        for idx, obj in enumerate(r.get('files') or [], 1):
            filename, size, file_id = obj.get('name'), obj.get('size'), obj.get('file_id')
            if not (isinstance(filename, str) and isinstance(size, int) and 0 < size <= MAX_BYTES
                    and isinstance(file_id, str) and file_id):
                continue
            result.append({
                'gpt': name, 'gpt_id': gid, 'filename': filename, 'file_id': file_id,
                'size': size, 'position': idx,
                'dest': root / f'{idx:02d}_{safe_part(filename)}',
            })
    return result


def source_files(vault, wanted_names, errors):
    """Walk only ordinary files. No symlink/junction traversal and no writes."""
    stack = [vault]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                items = list(it)
        except OSError as exc:
            errors.append(f'Не прочитана папка {folder}: {exc}')
            continue
        for ent in items:
            path = Path(ent.path)
            try:
                if is_reparse(path) or ent.is_symlink():
                    continue
                if ent.is_dir(follow_symlinks=False):
                    if ent.name.casefold() not in EXCLUDED_DIRS:
                        stack.append(path)
                elif ent.is_file(follow_symlinks=False) and norm(ent.name) in wanted_names:
                    s = ent.stat(follow_symlinks=False)
                    if 0 < s.st_size <= MAX_BYTES:
                        yield {'path':path, 'rel':str(path.relative_to(vault)).replace('\\', '/'),
                               'filename':ent.name, 'size':s.st_size}
            except OSError as exc:
                errors.append(f'Не прочитан объект {path}: {exc}')


def safe_source_path(vault, p):
    # Files can change during an hours-long scan. Recheck immediately before reading.
    if is_reparse(p) or not p.is_file():
        raise ValueError('Источник исчез, является ссылкой или недоступен')
    if not p.resolve().is_relative_to(vault):
        raise ValueError('Источник за пределами Obsidian-хранилища')


def exclusive_copy_and_verify(src, dest, size, digest, commit):
    """Copy only if absent; reject existing different bytes. Verify SHA after copy."""
    if dest.is_symlink() or is_reparse(dest) and dest.exists():
        return 'BLOCKED_LINK', 'Путь назначения является ссылкой'
    if dest.exists():
        if not dest.is_file():
            return 'BLOCKED_EXISTING', 'Путь назначения занят не файлом'
        if dest.stat().st_size == size and sha256(dest) == digest:
            return 'ALREADY_IDENTICAL', 'Совпадает размер и SHA-256'
        return 'BLOCKED_EXISTING', 'Там другой файл; перезапись запрещена'
    if not commit:
        return 'READY', 'Можно копировать с --commit'
    dest.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with src.open('rb') as fsrc, dest.open('xb') as fdst:
            created = True
            shutil.copyfileobj(fsrc, fdst, 1024 * 1024)
        if dest.stat().st_size != size or sha256(dest) != digest:
            dest.unlink(missing_ok=True)
            return 'COPY_VERIFY_FAILED', 'Созданный файл удалён: размер/SHA не совпал'
        return 'COPIED', 'Размер и SHA-256 после копирования совпадают'
    except OSError as exc:
        if created:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
        return 'COPY_ERROR', str(exc)


def report_target(t, status, message='', source=None, inbox=None, digest='', rule='', count=0):
    return {
        'GPT':t['gpt'], 'GPT_ID':t['gpt_id'], 'Knowledge':t['filename'],
        'File_ID':t['file_id'], 'Размер_ожидаемый':t['size'],
        'Найдено_вариантов':count, 'Источник_Obsidian':source['rel'] if source else '',
        'Inbox_копия':str(inbox) if inbox else '', 'Папка_Knowledge':str(t['dest']),
        'SHA256':digest, 'Правило_выбора':rule, 'Статус':status, 'Комментарий':message,
    }


def matches_gpt_folder(source, target):
    # Trust explicit directory component only, not fuzzy substring search.
    parts = [norm(x) for x in Path(source['rel']).parts[:-1]]
    variants = {norm(target['gpt_id']), norm(target['gpt']), norm(safe_part(target['gpt'], 68))}
    return bool(variants.intersection(parts))


def approved_mappings(review_path):
    result = set()
    if review_path.is_file():
        try:
            with review_path.open('r', newline='', encoding='utf-8-sig') as f:
                for row in csv.DictReader(f, delimiter=';'):
                    if norm(row.get('Подтвердить_ДА', '')) in ('да','yes','y','true','1'):
                        result.add((row.get('GPT_ID'), row.get('File_ID'), row.get('Файл_Obsidian'),
                                    row.get('SHA256')))
        except (OSError, ValueError) as exc:
            say('ВНИМАНИЕ: не прочитаны ручные подтверждения:', exc)
    return result


def assert_output_outside_vault(path, vault):
    """Prevent even a pre-existing output junction from redirecting writes into Vault."""
    resolved = path.resolve()
    if resolved.is_relative_to(vault):
        raise ValueError(f'ЗАПРЕЩЕНА ЗАПИСЬ В OBSIDIAN: {path} -> {resolved}')


def forbid_vault_output_for_all(vault, *paths):
    for p in paths:
        assert_output_outside_vault(p, vault)


def main(args):
    vault = Path(args.vault).expanduser().resolve()
    backup = Path(args.backup).expanduser().resolve()
    if not vault.is_dir():
        raise SystemExit(f'Не найдена папка Obsidian: {vault}\nУкажи путь через --vault "D:\\..."')
    if is_reparse(vault):
        raise SystemExit('Корневая папка хранилища является ссылкой/junction. Укажите физический путь.')
    if vault.is_relative_to(backup) or backup.is_relative_to(vault):
        raise SystemExit('ОШИБКА: Obsidian и резервная копия пересекаются. Выберите разные каталоги.')
    snapshot = Path(args.snapshot).expanduser().resolve() if args.snapshot else backup / 'gpt_list_snapshot.json'
    if not snapshot.is_file():
        raise SystemExit(f'Не найден snapshot: {snapshot}')
    if args.commit and not backup.is_dir():
        raise SystemExit(f'Не найдена папка резервной копии: {backup}')

    all_t = load_snapshot(snapshot, backup)
    missing = [t for t in all_t if not t['dest'].exists() and not t['dest'].is_symlink()]
    wrong_size = [t for t in all_t if t['dest'].is_file() and t['dest'].stat().st_size != t['size']]
    by_name = collections.defaultdict(list)
    by_name_size = collections.defaultdict(list)
    for t in missing:
        by_name[norm(t['filename'])].append(t)
        by_name_size[(norm(t['filename']), t['size'])].append(t)

    say('=== OBSIDIAN → My Downloads Arhiv → Knowledge конкретных GPT ===')
    say('ИСТОЧНИК (ТОЛЬКО ЧТЕНИЕ):', vault)
    say('BACKUP:', backup)
    say('Режим:', 'КОПИРОВАНИЕ (--commit)' if args.commit else 'ПРОВЕРКА БЕЗ КОПИРОВАНИЯ')
    say(f'Knowledge-записей: {len(all_t)}; отсутствуют: {len(missing)}; заняты неверным размером: {len(wrong_size)}')

    errors = []
    sources = list(source_files(vault, set(by_name), errors))
    # Keep only exact size matches; a same-name wrong-size source is a candidate for review, not an install.
    matched = collections.defaultdict(list)
    for s in sources:
        if by_name_size.get((norm(s['filename']),s['size'])):
            try:
                safe_source_path(vault, s['path'])
                s['sha256'] = sha256(s['path'])
                matched[(norm(s['filename']),s['size'])].append(s)
            except (OSError, ValueError) as exc:
                errors.append(f'Источник {s["rel"]}: {exc}')

    inbox_root = backup / 'My Downloads Arhiv' / '_AUTO_OBSIDIAN_FOUND'
    out = backup / 'Obsidian_Auto_Restore'
    forbid_vault_output_for_all(vault, inbox_root, out, backup / 'obsidian_knowledge_installed_receipts.jsonl')
    review_path = out / '04_РУЧНОЙ_ВЫБОР.csv'
    accepted = approved_mappings(review_path)
    output = []
    found_output = []
    staged = {}
    stage_counts = collections.Counter()
    # Stage all found candidates, even ambiguous ones, to preserve and inspect versions.
    for group in matched.values():
        for s in group:
            rel_id = hashlib.sha256(s['rel'].encode('utf-8')).hexdigest()[:20]
            inbox = inbox_root / rel_id / s['filename']
            forbid_vault_output_for_all(vault, inbox)
            status = 'READY'
            desc = 'Запланирована копия в My Downloads Arhiv'
            if args.commit:
                try:
                    safe_source_path(vault, s['path'])
                    if s['path'].stat().st_size != s['size'] or sha256(s['path']) != s['sha256']:
                        raise ValueError('Источник изменился со времени сканирования')
                    status, desc = exclusive_copy_and_verify(s['path'], inbox, s['size'], s['sha256'], True)
                except (OSError, ValueError) as exc:
                    status, desc = 'SOURCE_CHANGED_OR_ERROR', str(exc)
            else:
                status, desc = exclusive_copy_and_verify(s['path'], inbox, s['size'], s['sha256'], False)
            s['inbox'] = inbox
            s['stage_status'] = status
            stage_counts[status] += 1
            staged[s['rel']] = s
            found_output.append({'Файл_Obsidian':s['rel'], 'Имя':s['filename'], 'Размер':s['size'],
                                 'SHA256':s['sha256'], 'Inbox_копия':str(inbox),
                                 'Статус':status, 'Комментарий':desc})

    review_rows = []
    receipts = backup / 'obsidian_knowledge_installed_receipts.jsonl'
    installed = 0
    for t in missing:
        group = matched.get((norm(t['filename']),t['size']), [])
        if not group:
            same_name = [s for s in sources if norm(s['filename'])==norm(t['filename'])]
            st = 'FOUND_WRONG_SIZE_ONLY' if same_name else 'NOT_IN_OBSIDIAN'
            output.append(report_target(t, st, 'Не найден файл с точным именем и размером'))
            continue
        digests = {s['sha256'] for s in group}
        targets_same = by_name_size[(norm(t['filename']),t['size'])]
        rule = ''
        selected = None
        approved = [s for s in group if (t['gpt_id'],t['file_id'],s['rel'],s['sha256']) in accepted]
        hinted = [s for s in group if matches_gpt_folder(s,t)]
        if approved:
            if len({s['sha256'] for s in approved}) == 1:
                selected, rule = approved[0], 'USER_APPROVED_IN_CSV'
        elif (hinted and len({s['sha256'] for s in hinted}) == 1
              and sum(x['gpt_id'] == t['gpt_id'] for x in targets_same) == 1):
            selected, rule = hinted[0], 'EXACT_GPT_DIRECTORY_HINT'
        elif len(targets_same) == 1 and len(digests) == 1:
            selected, rule = group[0], 'UNIQUE_NAME_AND_SIZE'

        if selected is None:
            reason = ('Разные версии с одинаковым именем и размером'
                      if len(digests)>1 else 'Несколько GPT подходят по имени/размеру')
            output.append(report_target(t, 'NEEDS_MANUAL_MAPPING', reason, count=len(group)))
            for s in group:
                review_rows.append({'GPT':t['gpt'], 'GPT_ID':t['gpt_id'], 'Knowledge':t['filename'],
                                    'File_ID':t['file_id'], 'Размер_ожидаемый':t['size'],
                                    'Файл_Obsidian':s['rel'], 'SHA256':s['sha256'],
                                    'Причина':reason,
                                    'Подтвердить_ДА':'ДА' if (t['gpt_id'],t['file_id'],s['rel'],s['sha256']) in accepted else ''})
            continue

        if selected['stage_status'] not in ('COPIED','ALREADY_IDENTICAL','READY'):
            output.append(report_target(t, 'INBOX_COPY_FAILED', selected['stage_status'], selected,
                                        selected['inbox'], selected['sha256'], rule, len(group)))
            continue
        if not args.commit:
            output.append(report_target(t, 'READY_TO_INSTALL', 'Проверка пройдена, потребуется --commit',
                                        selected, selected['inbox'], selected['sha256'], rule, len(group)))
            continue
        # ALWAYS install from My Downloads Arhiv, NEVER directly from Obsidian.
        inbox = selected['inbox']
        if not inbox.is_file() or inbox.is_symlink() or inbox.stat().st_size != t['size'] or sha256(inbox) != selected['sha256']:
            output.append(report_target(t, 'INBOX_VERIFY_FAILED', 'Inbox-файл отсутствует либо его SHA не совпадает',
                                        selected, inbox, selected['sha256'], rule, len(group)))
            continue
        forbid_vault_output_for_all(vault, t['dest'])
        status, message = exclusive_copy_and_verify(inbox,t['dest'],t['size'],selected['sha256'],True)
        if status == 'COPIED':
            receipt = {
                'status':RECEIPT_STATUS, 'is_direct_gpt_original':False,
                'source_claim':SOURCE_CLAIM, 'gpt_name':t['gpt'], 'gpt_id':t['gpt_id'],
                'file_id':t['file_id'], 'file_name':t['filename'], 'expected_bytes':t['size'],
                'sha256':selected['sha256'], 'source_vault_path':str(selected['path']),
                'source_vault_relative_path':selected['rel'], 'inbox_path':str(inbox),
                'installed_path':str(t['dest']), 'matching_rule':rule,
                'installed_utc':datetime.now(timezone.utc).isoformat(),
                'warning':'Совпадение названия, размера и SHA копии НЕ доказывает идентичность файлу GPT из ChatGPT.'
            }
            try:
                receipts.parent.mkdir(parents=True,exist_ok=True)
                with receipts.open('a',encoding='utf-8') as f:
                    f.write(json.dumps(receipt,ensure_ascii=False)+'\n')
            except OSError as exc:
                status='COPIED_RECEIPT_ERROR'
                message=f'Файл СКОПИРОВАН, но журнал не записан: {exc}'
            installed += 1
        elif status == 'ALREADY_IDENTICAL':
            status='ALREADY_IN_GPT_IDENTICAL'
        output.append(report_target(t, status, message, selected, inbox,
                                    selected['sha256'], rule, len(group)))
        if status == 'COPIED':
            say(f'  + {t["gpt"]} / {t["filename"]} ({t["size"]} байт)')

    # Preserve manual approval flags for outstanding rows across runs.
    out.mkdir(parents=True,exist_ok=True)
    write_csv(out/'01_РЕЗУЛЬТАТ_ПО_GPT.csv', output, REPORT_FIELDS)
    write_csv(out/'02_НАЙДЕНО_В_OBSIDIAN.csv', found_output, FOUND_FIELDS)
    write_csv(review_path, review_rows, REVIEW_FIELDS)
    (out/'03_СВОДКА.json').write_text(json.dumps({
        'vault':str(vault), 'source_is_read_only':True, 'committed':bool(args.commit),
        'total_snapshot':len(all_t), 'missing_at_start':len(missing),
        'wrong_size_existing_not_touched':len(wrong_size), 'found_matching_source_files':len(found_output),
        'installed_into_gpt':installed, 'status_counts':dict(collections.Counter(x['Статус'] for x in output)),
        'staging_counts':dict(stage_counts), 'scan_errors':errors,
        'original_gpt_files_overwritten':0,
        'note':'Все новые файлы в GPT из Obsidian — неподтверждённые кандидаты, не прямые оригиналы.',
    },ensure_ascii=False,indent=2),encoding='utf-8')
    counts=collections.Counter(x['Статус'] for x in output)
    say('Найдено подходящих файлов в Vault:',len(found_output))
    say('Новых файлов в каталоги GPT:',installed)
    for key, number in sorted(counts.items()):
        if number:
            say(f'  {key}: {number}')
    say('Отчёты:',out)
    say('Журнал происхождения:',receipts)
    say('Хранилище Obsidian НЕ ИЗМЕНЯЛОСЬ. Уже сохранённые Knowledge НЕ ПЕРЕЗАПИСЫВАЛИСЬ.')
    if counts.get('NEEDS_MANUAL_MAPPING',0):
        say('Для неоднозначных документов: открой 04_РУЧНОЙ_ВЫБОР.csv, поставь ДА у нужных строк и запусти --commit повторно.')
    if errors:
        say(f'Предупреждение: ошибок чтения Vault: {len(errors)}; детали в 03_СВОДКА.json')
    return 0


def cli():
    ap=argparse.ArgumentParser(description='Офлайн-поиск недостающих GPT Knowledge в хранилище Obsidian без изменения хранилища.')
    ap.add_argument('--vault',default=DEFAULT_VAULT,help=f'Папка Obsidian; по умолчанию {DEFAULT_VAULT}')
    ap.add_argument('--backup',default=DEFAULT_BACKUP,help=f'Каталог GPT backup; по умолчанию {DEFAULT_BACKUP}')
    ap.add_argument('--snapshot',default=None,help='По умолчанию BACKUP/gpt_list_snapshot.json')
    mode=ap.add_mutually_exclusive_group()
    mode.add_argument('--commit',action='store_true',help='Копировать Vault → My Downloads Arhiv → Knowledge конкретных GPT')
    mode.add_argument('--dry-run',action='store_true',help='Только проверить (по умолчанию)')
    return main(ap.parse_args())

if __name__=='__main__':
    sys.exit(cli())
