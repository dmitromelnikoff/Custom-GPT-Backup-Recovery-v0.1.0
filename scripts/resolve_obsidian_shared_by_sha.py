#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверить SHA-256 в 04_РУЧНОЙ_ВЫБОР.csv и автоматически разрешить
одну общую копию Obsidian для нескольких GPT. Хранилище — только чтение.
Без --commit: только аудит. С --commit: заполняет ДА и вызывает уже
установленный auto_restore_knowledge_from_obsidian.py --commit.
"""
from __future__ import annotations

import argparse
import collections
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unicodedata

DEFAULT_VAULT = r'X:\Vault\GPT_Assistents'
DEFAULT_BACKUP = 'Custom_GPT_backup'
REVIEW_NAME = '04_РУЧНОЙ_ВЫБОР.csv'
HASH = re.compile(r'^[0-9a-fA-F]{64}$')


def norm(x):
    return unicodedata.normalize('NFC', str(x)).casefold().strip()


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def read_csv(p):
    with p.open('r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f, delimiter=';')
        rows = list(reader)
        fields = reader.fieldnames or []
    required = {'GPT', 'GPT_ID', 'Knowledge', 'File_ID', 'Размер_ожидаемый',
                'Файл_Obsidian', 'SHA256', 'Подтвердить_ДА'}
    if not required.issubset(fields):
        raise ValueError(f'В CSV отсутствуют обязательные колонки: {sorted(required - set(fields))}')
    return rows, fields


def write_csv_atomic(path, rows, fields):
    # Only in BACKUP, never in VAULT; no overwrite of original without backup.
    temp = path.with_name(path.name + '.sha_approval_tmp')
    try:
        with temp.open('x', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields, delimiter=';', extrasaction='ignore')
            w.writeheader()
            w.writerows(rows)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def get_snapshot_index(path):
    snap = json.loads(path.read_text(encoding='utf-8-sig'))
    data = snap.get('gpts', []) if isinstance(snap, dict) else snap
    idx = {}
    for item in data:
        r = item.get('resource', item)
        g = r.get('gizmo', r)
        gid = g.get('id', '')
        name = (g.get('display') or {}).get('name') or gid
        for f in r.get('files') or []:
            fid = f.get('file_id')
            if gid and fid:
                idx[(gid, fid)] = (name, f.get('name'), f.get('size'))
    return idx


def make_report(path, report_rows):
    fields = ['GPT', 'GPT_ID', 'Knowledge', 'File_ID', 'Файл_Obsidian',
              'CSV_SHA256', 'Фактический_SHA256', 'Статус', 'Причина']
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(report_rows)


def main():
    ap = argparse.ArgumentParser(description='Офлайн SHA-256 проверка 41 неоднозначных Knowledge и массовое подтверждение общих источников.')
    ap.add_argument('--vault', default=DEFAULT_VAULT, help=f'Хранилище Obsidian, по умолчанию {DEFAULT_VAULT}')
    ap.add_argument('--backup', default=DEFAULT_BACKUP, help=f'Каталог резервной копии, по умолчанию {DEFAULT_BACKUP}')
    ap.add_argument('--csv', default=None, help='Файл 04_РУЧНОЙ_ВЫБОР.csv; по умолчанию BACKUP/Obsidian_Auto_Restore/...')
    ap.add_argument('--commit', action='store_true', help='После проверки заполнить ДА и вызвать прежний установщик')
    args = ap.parse_args()
    vault = Path(args.vault).resolve(strict=True)
    backup = Path(args.backup).resolve(strict=True)
    if not vault.is_dir():
        raise ValueError(f'Нет хранилища: {vault}')
    if backup == vault or backup.is_relative_to(vault):
        raise ValueError('Каталог backup внутри Obsidian! Запись запрещена.')
    csv_path = (Path(args.csv) if args.csv else backup/'Obsidian_Auto_Restore'/REVIEW_NAME).resolve(strict=True)
    if csv_path.is_relative_to(vault):
        raise ValueError('CSV находится внутри Obsidian — запись запрещена.')
    rows, fields = read_csv(csv_path)
    idx = get_snapshot_index(backup/'gpt_list_snapshot.json')
    groups = collections.defaultdict(list)
    for row in rows:
        groups[(norm(row['Knowledge']), str(row['Размер_ожидаемый']).strip())].append(row)
    # Rehash each distinct actual file once; every row's CSV hash is only a claim.
    cache = {}
    report = []
    valid = set()
    statuses = collections.Counter()
    for row in rows:
        original_sha = str(row['SHA256']).strip().lower()
        rel = str(row['Файл_Obsidian']).strip().replace('\\', '/')
        reason = ''
        actual_sha = ''
        try:
            if not HASH.fullmatch(original_sha):
                raise ValueError('Некорректный SHA-256 в CSV')
            group = groups[(norm(row['Knowledge']), str(row['Размер_ожидаемый']).strip())]
            if len({r['SHA256'].lower().strip() for r in group}) != 1:
                raise ValueError('CONFLICT_SHA: разные SHA для одного имени/размера')
            ref = idx.get((row['GPT_ID'], row['File_ID']))
            if not ref:
                raise ValueError('GPT_ID + File_ID не найдены в snapshot')
            name, filename, size = ref
            expected_size = int(row['Размер_ожидаемый'])
            if norm(name) != norm(row['GPT']) or norm(filename) != norm(row['Knowledge']) or size != expected_size:
                raise ValueError('Метаданные CSV расходятся с snapshot')
            relpath = Path(rel)
            if relpath.is_absolute() or '..' in relpath.parts or not relpath.parts:
                raise ValueError('Небезопасный относительный путь')
            full = vault / relpath
            real = full.resolve(strict=True)
            if not real.is_relative_to(vault) or full.is_symlink() or not real.is_file():
                raise ValueError('Путь вышел из Vault, является ссылкой или не файлом')
            if norm(full.name) != norm(row['Knowledge']):
                raise ValueError('Имя исходного файла отличается')
            if real.stat().st_size != expected_size:
                raise ValueError(f'Размер источника изменился: {real.stat().st_size} != {expected_size}')
            if real not in cache:
                cache[real] = sha256(real)
            actual_sha = cache[real]
            if actual_sha != original_sha:
                raise ValueError('SHA-256 источника отличается от CSV')
            status = 'SHA_VERIFIED'
            valid.add((row['GPT_ID'], row['File_ID'], row['Файл_Obsidian'], row['SHA256']))
        except (OSError, ValueError) as exc:
            status = 'SKIPPED'
            reason = str(exc)
        statuses[status] += 1
        report.append({'GPT':row['GPT'], 'GPT_ID':row['GPT_ID'], 'Knowledge':row['Knowledge'],
                       'File_ID':row['File_ID'], 'Файл_Obsidian':row['Файл_Obsidian'],
                       'CSV_SHA256':row['SHA256'], 'Фактический_SHA256':actual_sha,
                       'Статус':status, 'Причина':reason})
    report_path = backup/'Obsidian_Auto_Restore'/'05_АВТОПРОВЕРКА_SHA256.csv'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    make_report(report_path, report)
    print(f'Записей в очереди: {len(rows)}; исходных файлов: {len(cache)}')
    print(f'SHA-256 проверен по реальным байтам: {statuses["SHA_VERIFIED"]}; пропущено: {statuses["SKIPPED"]}')
    if statuses['SKIPPED']:
        for r in report:
            if r['Статус'] == 'SKIPPED':
                print(f'  ! {r["GPT"]} / {r["Knowledge"]}: {r["Причина"]}')
    print('Отчёт:', report_path)
    if not args.commit:
        print('ПРОВЕРКА: файлы НЕ копировались. Для установки запусти с --commit.')
        return 0
    if not valid:
        print('Очередь пуста: устанавливать нечего (возможно, всё уже скопировано).')
        return 0 if not rows else 2
    if statuses['SKIPPED']:
        print('Есть несовпадения SHA. Для безопасности установка отменена. Сначала разбери отчёт.')
        return 2
    orig = csv_path.with_name(csv_path.stem + '.before_auto_sha_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.csv')
    # If name already exists, refuse rather than overwrite.
    with orig.open('xb') as f:
        f.write(csv_path.read_bytes())
    approved = 0
    for r in rows:
        k = (r['GPT_ID'], r['File_ID'], r['Файл_Obsidian'], r['SHA256'])
        if k in valid:
            r['Подтвердить_ДА'] = 'ДА'
            approved += 1
    write_csv_atomic(csv_path, rows, fields)
    print(f'Подтверждено {approved} строк. Резервная копия исходного CSV: {orig}')
    old_script = Path(__file__).resolve().parent/'auto_restore_knowledge_from_obsidian.py'
    if not old_script.is_file():
        print(f'Не найден установщик: {old_script}; CSV уже подтверждён, можно запустить его вручную с --commit.')
        return 2
    command = [sys.executable, str(old_script), '--vault', str(vault), '--backup', str(backup), '--commit']
    print('Запускаю установку: Obsidian (только чтение) → My Downloads Arhiv → папки GPT', flush=True)
    completed = subprocess.run(command, check=False)
    return completed.returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print('ОШИБКА:', exc, file=sys.stderr)
        sys.exit(2)
