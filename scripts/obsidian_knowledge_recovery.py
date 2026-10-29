#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Офлайн-поиск недостающих Knowledge по GPT + осторожный приём файлов из Obsidian.

Команды:
    python obsidian_knowledge_recovery.py report
    python obsidian_knowledge_recovery.py scan
    python obsidian_knowledge_recovery.py stage

НИКОГДА не записывает файлы в gpts/.../knowledge, не делает сетевых запросов.
Архивные документы сохраняются только как НЕПОДТВЕРЖДЁННЫЕ КАНДИДАТЫ.
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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024 * 1024
SIZE_LIMIT_GPT = 512 * 1024 * 1024
SAVED_STATUS = 'SAVED_DIRECT_GPT_EDITOR_SIZE_MATCH'
FIELDS_MISSING = [
    'GPT', 'GPT_ID', 'Knowledge', 'Размер_байт', 'File_ID', 'Позиция',
    'Статус_отложенного', 'Кандидат_общей_базы', 'Имя_повторяется_в_GPT',
    'Ожидаемый_путь',
]
FIELDS_REVIEW = [
    'Найденный_файл', 'GPT', 'GPT_ID', 'File_ID', 'Knowledge',
    'Размер_ожидаемый', 'Размер_фактический', 'SHA256',
    'Причина', 'Подтвердить_ДА',
]
FIELDS_SCAN = [
    'Найденный_файл', 'Размер_байт', 'SHA256', 'Статус', 'GPT_кандидаты',
    'Knowledge_кандидаты', 'Количество_кандидатов', 'Комментарий',
]


def say(*args):
    print(*args, flush=True)


def norm(text):
    return unicodedata.normalize('NFC', str(text)).casefold().strip()


def safe_part(s, limit=100):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        s = '_' + s
    return s


def csvwrite(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fields, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def csvread(path):
    with path.open('r', newline='', encoding='utf-8-sig') as f:
        return list(csv.DictReader(f, delimiter=';'))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def load_snapshot(path, backup):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
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
            fn = obj.get('name')
            size = obj.get('size')
            if not isinstance(fn, str) or not isinstance(size, int) or not 0 < size < SIZE_LIMIT_GPT:
                continue
            fid = obj.get('file_id') or ''
            result.append({
                'gpt': name, 'gpt_id': gid, 'filename': fn, 'file_id': fid,
                'size': size, 'position': idx, 'dest': root / f'{idx:02d}_{safe_part(fn)}',
                'created_at': obj.get('created_at') or '',
                'last_modified': obj.get('last_modified') or '',
            })
    return result


def load_deferred(backup):
    p = backup / 'direct_gpt_deferred_originals.json'
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding='utf-8-sig'))
        return {(it.get('gpt_id'), it.get('file_id')): str(it.get('status') or '')
                for it in d.get('items', []) if isinstance(it, dict)}
    except (OSError, ValueError, TypeError):
        return {}


def direct_receipts(backup):
    receipts = {}
    p = backup / 'direct_gpt_ui_knowledge_inventory.csv'
    if p.is_file():
        try:
            for rec in csvread(p):
                if rec.get('status') != SAVED_STATUS:
                    continue
                m = re.search(r'SHA-256\s+([a-fA-F0-9]{64})\b', rec.get('note') or '')
                if m:
                    receipts[(rec.get('gpt_id'), rec.get('file_id'))] = m.group(1).lower()
        except (OSError, ValueError, TypeError) as exc:
            say(f'ВНИМАНИЕ: не удалось прочитать прямой журнал: {exc}')
    p = backup / 'direct_gpt_knowledge_import_receipts.jsonl'
    if p.is_file():
        with p.open(encoding='utf-8-sig') as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get('result') not in ('RESTORED_FROM_DIRECT_GPT_DOWNLOAD', 'ALREADY_PRESENT_EXACT_HASH'):
                        continue
                    if rec.get('source_claim') != 'user_manual_download_from_GPT_Knowledge':
                        continue
                    digest = (rec.get('original') or {}).get('sha256') or ''
                    if re.fullmatch(r'[0-9a-fA-F]{64}', digest):
                        receipts[(rec.get('gpt_id'), rec.get('file_id'))] = digest.lower()
                except (ValueError, TypeError):
                    continue
    return receipts


def shared_candidate(backup, item):
    p = backup / 'Shared_Knowledge_candidates' / safe_part(item['gpt_id'], 90) / safe_part(item['file_id'], 90)
    if not p.is_dir():
        return False
    for meta in p.glob('*.json'):
        try:
            obj = json.loads(meta.read_text(encoding='utf-8'))
            if obj.get('status') != 'SHARED_SOURCE_CANDIDATE_NOT_TARGET_ORIGINAL':
                continue
            target = obj.get('target') or {}
            if (target.get('gpt_id'), target.get('file_id')) != (item['gpt_id'], item['file_id']):
                continue
            digest = obj.get('candidate_sha256') or ''
            dat = p / f'{digest}.bin'
            if (re.fullmatch(r'[0-9a-f]{64}', digest) and dat.is_file()
                and dat.stat().st_size == item['size'] and sha(dat) == digest):
                return True
        except (OSError, ValueError, TypeError):
            continue
    return False


def collect(args):
    backup = Path(args.backup).expanduser().resolve()
    snapshot = Path(args.snapshot).expanduser().resolve() if args.snapshot else backup / 'gpt_list_snapshot.json'
    if not snapshot.is_file():
        raise SystemExit(f'Нет snapshot: {snapshot}')
    out = Path(args.out).expanduser().resolve() if args.out else backup / 'Obsidian_Knowledge_recovery'
    inbox = Path(args.inbox).expanduser().resolve() if args.inbox else backup / 'My Downloads Arhiv'
    inbox.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    all_items = load_snapshot(snapshot, backup)
    deferred, receipts = load_deferred(backup), direct_receipts(backup)
    names_in_gpt = collections.Counter((r['gpt_id'], norm(r['filename'])) for r in all_items)
    missing, size_only, confirmed = [], [], []
    for item in all_items:
        key = (item['gpt_id'], item['file_id'])
        dest = item['dest']
        if dest.is_file() and dest.stat().st_size == item['size']:
            dig = receipts.get(key)
            if dig and sha(dest) == dig:
                confirmed.append(item)
            else:
                size_only.append(item)
        else:
            q = dict(item)
            q['deferred'] = deferred.get(key, '')
            q['shared'] = shared_candidate(backup, item)
            q['within_gpt_duplicate'] = names_in_gpt[(item['gpt_id'], norm(item['filename']))] > 1
            q['path_exists_wrong_size'] = dest.is_file()
            missing.append(q)
    return backup, out, inbox, all_items, missing, size_only, confirmed


def main_report(args):
    backup, out, inbox, all_items, missing, size_only, confirmed = collect(args)
    missing.sort(key=lambda r: (norm(r['gpt']), norm(r['filename']), r['position']))
    existing = collections.defaultdict(list)
    for r in missing:
        existing[r['gpt_id']].append(r)
    summary = sorted(existing.values(), key=lambda gg: (-len(gg), norm(gg[0]['gpt'])))
    rows = []
    for item in missing:
        rows.append({
            'GPT': item['gpt'], 'GPT_ID': item['gpt_id'],
            'Knowledge': item['filename'], 'Размер_байт': item['size'],
            'File_ID': item['file_id'], 'Позиция': item['position'],
            'Статус_отложенного': item['deferred'],
            'Кандидат_общей_базы': 'ДА (НЕ оригинал)' if item['shared'] else 'НЕТ',
            'Имя_повторяется_в_GPT': 'ДА' if item['within_gpt_duplicate'] else 'НЕТ',
            'Ожидаемый_путь': str(item['dest']),
        })
    csvwrite(out / '01_НЕДОСТАЮЩИЕ_KNOWLEDGE_ПО_GPT.csv', rows, FIELDS_MISSING)
    grouped_by_name = collections.defaultdict(list)
    for item in missing:
        grouped_by_name[norm(item['filename'])].append(item)
    search_rows = []
    for k, group in sorted(grouped_by_name.items(), key=lambda x: x[0]):
        search_rows.append({
            'Имя_для_поиска_в_Obsidian': group[0]['filename'],
            'Количество_недостающих_записей': len(group),
            'GPT_названия': ' | '.join(sorted(set(g['gpt'] for g in group), key=norm)),
            'Размеры_байт': ' | '.join(map(str, sorted(set(g['size'] for g in group)))),
            'Где_есть_общий_кандидат': sum(bool(g['shared']) for g in group),
            'Разные_File_ID': len({g['file_id'] for g in group}),
        })
    csvwrite(out / '02_ПОИСК_ПО_ИМЕНАМ_Obsidian.csv', search_rows,
             ['Имя_для_поиска_в_Obsidian', 'Количество_недостающих_записей', 'GPT_названия',
              'Размеры_байт', 'Где_есть_общий_кандидат', 'Разные_File_ID'])
    review_rows = [
        {'GPT':r['gpt'], 'GPT_ID':r['gpt_id'], 'Knowledge':r['filename'],
         'Размер_байт':r['size'], 'File_ID':r['file_id'],
         'Ожидаемый_путь':str(r['dest']), 'Статус':'РАЗМЕР_СОВПАДАЕТ_ПРОИСХОЖДЕНИЕ_НЕ_ПОДТВЕРЖДЕНО'}
        for r in sorted(size_only, key=lambda x: (norm(x['gpt']), norm(x['filename'])))
    ]
    csvwrite(out / '03_ПРОВЕРИТЬ_СТАРЫЕ_LIBRARY_И_НЕПОДТВЕРЖДЕННЫЕ.csv', review_rows,
             ['GPT','GPT_ID','Knowledge','Размер_байт','File_ID','Ожидаемый_путь','Статус'])
    csvwrite(out / '04_СВОДКА_ПО_GPT.csv', [
        {'GPT':grp[0]['gpt'], 'GPT_ID':grp[0]['gpt_id'], 'Недостаёт':len(grp),
         'Есть_общие_кандидаты':sum(bool(item['shared']) for item in grp),
         'Отложено_ошибками':sum(bool(item['deferred']) for item in grp)} for grp in summary
    ], ['GPT','GPT_ID','Недостаёт','Есть_общие_кандидаты','Отложено_ошибками'])
    lines = [
        '# Недостающие Knowledge — список для поиска в Obsidian', '',
        f'Снимок: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}', '',
        f'**Всего Knowledge:** {len(all_items)} · **Подтверждено напрямую:** {len(confirmed)} · '
        f'**Совпадает только размер:** {len(size_only)} · **Недостаёт:** {len(missing)} · '
        f'**GPT с недостающими:** {len(summary)}', '',
        'Файлы, найденные в Obsidian, складывай в `Custom_GPT_backup/My Downloads Arhiv/`. '
        'Не переименовывай их без необходимости; можно оставлять подпапки.', '',
        '**Важно:** совпадение имени и размера не доказывает идентичность экземпляру Knowledge GPT. '
        'Импортёр создаёт отдельные кандидатные копии без замены оригиналов.', '',
    ]
    for grp in summary:
        lines.extend([f'## {grp[0]["gpt"]} — {len(grp)} файлов',
                      f'GPT ID: `{grp[0]["gpt_id"]}`', ''])
        for r in grp:
            suffix = (' · есть кандидат из другого GPT' if r['shared'] else '')
            if r['deferred']:
                suffix += f' · отложен ({r["deferred"]})'
            if r['within_gpt_duplicate']:
                suffix += ' · имя повторяется внутри GPT'
            fn = r['filename'].replace('`', '\\`')
            lines.append(f'- [ ] `{fn}` — **{r["size"]:,} байт** · file_id `{r["file_id"]}`{suffix}')
        lines.append('')
    (out / '00_ЧЕКЛИСТ_ДЛЯ_OBSIDIAN.md').write_text('\n'.join(lines), encoding='utf-8')
    totals = {
        'total':len(all_items), 'verified_direct_sha_receipts':len(confirmed),
        'size_match_only':len(size_only), 'missing':len(missing),
        'gpts_with_missing':len(summary),
        'missing_with_shared_candidates':sum(bool(r['shared']) for r in missing),
        'inbox':str(inbox), 'out':str(out),
        'warning':'Статусы зависят от актуальных файлов на локальном диске. Архив Obsidian — лишь кандидат, пока не доказано соответствие GPT.'
    }
    (out / 'report_summary.json').write_text(json.dumps(totals, ensure_ascii=False, indent=2), encoding='utf-8')
    say('=== СПИСОК ДЛЯ ПОИСКА В OBSIDIAN ===')
    say(f'Knowledge {len(all_items)}, прямых SHA-подтверждений {len(confirmed)}, только размер {len(size_only)}, не хватает {len(missing)}')
    say(f'GPT с пропусками: {len(summary)}; общих кандидатов: {totals["missing_with_shared_candidates"]}')
    say('Чеклист:', out / '00_ЧЕКЛИСТ_ДЛЯ_OBSIDIAN.md')
    say('Таблица по GPT:', out / '01_НЕДОСТАЮЩИЕ_KNOWLEDGE_ПО_GPT.csv')
    say('Поиск по именам:', out / '02_ПОИСК_ПО_ИМЕНАМ_Obsidian.csv')
    say('Отдельная проверка старых копий:', out / '03_ПРОВЕРИТЬ_СТАРЫЕ_LIBRARY_И_НЕПОДТВЕРЖДЕННЫЕ.csv')
    say('Папка найденного:', inbox)
    say('Сеть и GPT Knowledge не использовались.')


def inbox_files(inbox):
    for p in sorted(inbox.rglob('*'), key=lambda p: norm(str(p))):
        if p.is_symlink() or not p.is_file():
            continue
        if p.name.startswith('~$'):
            continue
        yield p


def scan_inbox(args, stage=False):
    backup, out, inbox, all_items, missing, size_only, confirmed = collect(args)
    if not (out / 'report_summary.json').is_file():
        # Do not auto-generate report in stage, but always show where to start.
        say('Совет: сначала выполните команду report для чеклиста.')
    by_name = collections.defaultdict(list)
    for r in missing:
        by_name[norm(r['filename'])].append(r)
    scan_rows, review_rows = [], []
    archive_files = []
    try:
        archive_files = list(inbox_files(inbox))
    except OSError as exc:
        raise SystemExit(f'Ошибка чтения папки {inbox}: {exc}')
    for p in archive_files:
        rel = str(p.relative_to(inbox)).replace('\\','/')
        size = p.stat().st_size
        if not 0 < size <= MAX_FILE_BYTES:
            scan_rows.append({'Найденный_файл':rel, 'Размер_байт':size,
                              'Статус':'SKIP_EMPTY_OR_TOO_BIG', 'Комментарий':'Проверить вручную'})
            continue
        digest = sha(p)
        matches_name = by_name.get(norm(p.name), [])
        equal = [r for r in matches_name if r['size'] == size]
        prefix_matches = []
        if not matches_name and re.match(r'^\d{2}_', p.name):
            prefix_matches = [r for r in by_name.get(norm(p.name[3:]),[]) if r['size']==size]
        if len(equal) == 1:
            status, explanation, candidates = ('UNIQUE_EXACT_NAME_SIZE', 'Один целевой GPT по имени+размеру; всё равно НЕ доказанный оригинал GPT', equal)
        elif len(equal) > 1:
            status, explanation, candidates = ('MULTIPLE_POSSIBLE_GPTS', 'Одинаковое имя/размер в нескольких GPT: требуется отметка ДА в таблице выбора', equal)
        elif matches_name:
            status, explanation, candidates = ('SIZE_MISMATCH', 'Имя найдено, но размер не совпал ни с одним недостающим Knowledge', matches_name)
        elif prefix_matches:
            status, explanation, candidates = ('BACKUP_PREFIX_REVIEW', 'Похоже на префикс номера из резервной копии: проверить вручную', prefix_matches)
        else:
            status, explanation, candidates = ('NO_FILENAME_MATCH', 'Нет подходящего имени среди недостающих Knowledge', [])
        scan_rows.append({
            'Найденный_файл':rel, 'Размер_байт':size, 'SHA256':digest,
            'Статус':status, 'GPT_кандидаты':' | '.join(sorted(set(r['gpt'] for r in candidates),key=norm)),
            'Knowledge_кандидаты':' | '.join(sorted(set(r['filename'] for r in candidates),key=norm)),
            'Количество_кандидатов':len(candidates), 'Комментарий':explanation,
        })
        if status in ('MULTIPLE_POSSIBLE_GPTS', 'SIZE_MISMATCH', 'BACKUP_PREFIX_REVIEW'):
            for r in candidates:
                review_rows.append({
                    'Найденный_файл':rel, 'GPT':r['gpt'], 'GPT_ID':r['gpt_id'],
                    'File_ID':r['file_id'], 'Knowledge':r['filename'],
                    'Размер_ожидаемый':r['size'], 'Размер_фактический':size,
                    'SHA256':digest, 'Причина':status, 'Подтвердить_ДА':'',
                })
    csvwrite(out/'05_СКАН_MY_DOWNLOADS_ARHIV.csv',scan_rows,FIELDS_SCAN)
    approval_file = out/'06_РУЧНОЕ_СОПОСТАВЛЕНИЕ.csv'
    # При повторном scan добавляем новые неоднозначные сопоставления, но
    # не теряем ранее проставленные пользователем «ДА».
    prior = {}
    if approval_file.is_file():
        try:
            for old in csvread(approval_file):
                key = (old.get('Найденный_файл'), old.get('GPT_ID'),
                       old.get('File_ID'), old.get('SHA256'))
                prior[key] = old.get('Подтвердить_ДА','')
        except (OSError, ValueError):
            pass
    for row in review_rows:
        key = (row['Найденный_файл'], row['GPT_ID'], row['File_ID'], row['SHA256'])
        row['Подтвердить_ДА'] = prior.get(key, '')
    csvwrite(approval_file, review_rows, FIELDS_REVIEW)
    summary = collections.Counter(r['Статус'] for r in scan_rows)
    say('=== ПРОВЕРКА ФАЙЛОВ ИЗ My Downloads Arhiv ===')
    say('Файлов в папке:', len(archive_files))
    for k,v in sorted(summary.items()):
        say(' ', k, v)
    say('Отчёт:', out/'05_СКАН_MY_DOWNLOADS_ARHIV.csv')
    say('Ручное сопоставление (сохраняет сделанные отметки):', approval_file)
    if stage:
        return stage_from_scan(args, backup, out, inbox, missing, scan_rows, approval_file)
    return summary


def stage_from_scan(args, backup, out, inbox, missing, scan_rows, approval_file):
    by_key = {(r['gpt_id'],r['file_id']):r for r in missing}
    by_name_size = collections.defaultdict(list)
    for r in missing:
        by_name_size[(norm(r['filename']),r['size'])].append(r)
    accepted = []
    for row in scan_rows:
        if row['Статус'] != 'UNIQUE_EXACT_NAME_SIZE':
            continue
        group = by_name_size[(norm(Path(row['Найденный_файл']).name),int(row['Размер_байт']))]
        if len(group) == 1:
            accepted.append((row['Найденный_файл'],group[0],'AUTOMATIC_UNIQUE_NAME_AND_SIZE'))
    if approval_file.is_file():
        for r in csvread(approval_file):
            if norm(r.get('Подтвердить_ДА','')) not in ('да','yes','y','true','1'):
                continue
            target = by_key.get((r.get('GPT_ID'),r.get('File_ID')))
            if target:
                accepted.append((r.get('Найденный_файл',''),target,'USER_APPROVED_AMBIGUOUS_MAPPING'))
    candidates_dir = backup/'Obsidian_Archive_candidates'
    imported = skipped = errors = 0
    manifest = []
    staged_unique = set()
    for rel, target, mapping_type in accepted:
        rel = rel.replace('\\', '/')
        source = (inbox / rel).resolve()
        try:
            source.relative_to(inbox.resolve())
            if not source.is_file():
                raise ValueError('Источник отсутствует')
            if source.stat().st_size != target['size']:
                raise ValueError('Размер не совпал с целевым Knowledge')
            # Exact source filename mandatory except explicitly approved backup ordinal.
            if norm(source.name) != norm(target['filename']):
                if not (mapping_type.startswith('USER_APPROVED') and
                        re.match(r'^\d{2}_',source.name) and norm(source.name[3:])==norm(target['filename'])):
                    raise ValueError('Имя файла отличается: нельзя привязать автоматически')
            digest = sha(source)
            key = (target['gpt_id'], target['file_id'], digest)
            if key in staged_unique:
                continue
            staged_unique.add(key)
            folder = candidates_dir / safe_part(target['gpt_id'],90) / safe_part(target['file_id'],90)
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f'{digest}.bin'
            meta_path = folder / f'{digest}.json'
            if dest.exists():
                if dest.stat().st_size != source.stat().st_size or sha(dest)!=digest:
                    raise ValueError('Уже существует кандидат с неверным SHA-256: перезапись запрещена')
                skipped += 1
                status = 'ALREADY_PRESENT_CANDIDATE'
            else:
                with source.open('rb') as fsrc, dest.open('xb') as fdst:
                    shutil.copyfileobj(fsrc,fdst,1024*1024)
                if sha(dest)!=digest:
                    dest.unlink(missing_ok=True)
                    raise ValueError('Сбой проверки SHA256 после копирования')
                imported += 1
                status = 'STAGED_OBSIDIAN_ARCHIVE_CANDIDATE_NOT_ORIGINAL'
            metadata = {
                'status':'OBSIDIAN_ARCHIVE_CANDIDATE_NOT_GPT_ORIGINAL',
                'source_claim':'FILE_FROM_USER_OBSIDIAN_OR_LOCAL_ARCHIVE_UNVERIFIED',
                'target':{'gpt_name':target['gpt'],'gpt_id':target['gpt_id'],
                          'file_id':target['file_id'],'file_name':target['filename'],
                          'expected_bytes':target['size']},
                'source_archive_relative_path':rel,
                'mapping_type':mapping_type,
                'candidate_sha256':digest,
                'candidate_bytes':target['size'],
                'candidate_path':str(dest),
                'staged_utc':datetime.now(timezone.utc).isoformat(),
                'warning':'Не подтверждено, что данный файл байтово идентичен Knowledge целевого GPT.',
            }
            if not meta_path.exists():
                with meta_path.open('x', encoding='utf-8') as f:
                    json.dump(metadata,f,ensure_ascii=False,indent=2)
            manifest.append({'Найденный_файл':rel,'GPT':target['gpt'],'GPT_ID':target['gpt_id'],
                             'Knowledge':target['filename'],'File_ID':target['file_id'],
                             'SHA256':digest,'Статус':status,'Кандидат':str(dest)})
        except (OSError,ValueError) as exc:
            errors += 1
            manifest.append({'Найденный_файл':rel,'GPT':target['gpt'],'GPT_ID':target['gpt_id'],
                             'Knowledge':target['filename'],'File_ID':target['file_id'],
                             'SHA256':'','Статус':f'НЕ_СОХРАНЕНО: {exc}','Кандидат':''})
    csvwrite(out/'07_РЕЗУЛЬТАТЫ_ИМПОРТА_КАНДИДАТОВ.csv',manifest,
             ['Найденный_файл','GPT','GPT_ID','Knowledge','File_ID','SHA256','Статус','Кандидат'])
    say('=== СОХРАНЕНИЕ КАНДИДАТОВ (НЕ КАНОНИЧЕСКИХ ОРИГИНАЛОВ) ===')
    say(f'Новых: {imported}; уже было: {skipped}; ошибок: {errors}; не сопоставленные/неоднозначные НЕ копировались')
    say('Каталог:', candidates_dir)
    say('Результат:', out/'07_РЕЗУЛЬТАТЫ_ИМПОРТА_КАНДИДАТОВ.csv')
    say('Папка gpts/.../knowledge не изменялась.')
    return imported


def main():
    parser = argparse.ArgumentParser(description='Офлайн-отчёт поиска Knowledge по GPT и обработка локальных файлов Obsidian')
    parser.add_argument('command', choices=['report','scan','stage'], help='report=список; scan=сверка папки; stage=сохранить безопасные копии-кандидаты')
    parser.add_argument('--backup', default='Custom_GPT_backup',help='Каталог резервной копии')
    parser.add_argument('--snapshot', default=None,help='gpt_list_snapshot.json; по умолчанию внутри --backup')
    parser.add_argument('--out', default=None,help='Каталог отчётов')
    parser.add_argument('--inbox', default=None,help='Каталог найденных файлов: My Downloads Arhiv')
    args = parser.parse_args()
    if args.command == 'report':
        main_report(args)
    elif args.command == 'scan':
        scan_inbox(args)
    else:
        scan_inbox(args,stage=True)


if __name__=='__main__':
    main()
