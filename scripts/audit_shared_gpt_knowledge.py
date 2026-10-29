#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline, read-only audit of likely shared Custom GPT Knowledge sources.

Uses GPT snapshot metadata and currently saved ORIGINAL Knowledge byte files.
NEVER copies or modifies Knowledge, never treats same size as proof of identical bytes.
No browser, no networking, no plugins.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path


def safe_part(s, limit=100):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        s = '_' + s
    return s


def sha256_file(p):
    sha = hashlib.sha256()
    with p.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            sha.update(chunk)
    return sha.hexdigest()


def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f, fieldnames=fields, delimiter=';', extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def load_entries(snapshot, backup):
    blob = json.loads(snapshot.read_text(encoding='utf-8-sig'))
    if not isinstance(blob, dict) or not isinstance(blob.get('gpts'), list):
        raise ValueError('Ожидался snapshot с полем gpts')
    entries = []
    for entry in blob['gpts']:
        r = entry.get('resource', entry)
        g = r.get('gizmo', r)
        gid = g.get('id')
        name = (g.get('display') or {}).get('name') or gid
        if not gid or not isinstance(name, str):
            continue
        for index, f in enumerate(r.get('files') or [], 1):
            fname = f.get('name')
            size = f.get('size')
            if not isinstance(fname, str) or not isinstance(size, int) or size <= 0:
                continue
            dest = backup / 'gpts' / safe_part(name, 68) / safe_part(gid, 90) / 'knowledge' / f'{index:02d}_{safe_part(fname)}'
            actual = dest.stat().st_size if dest.is_file() else 0
            file_status = 'SIZE_MATCH_PRESENT' if actual == size else ('WRONG_SIZE_PRESENT' if actual else 'MISSING')
            entries.append({
                'gpt_id':gid, 'gpt_name':name, 'name':fname, 'file_id':f.get('file_id') or '',
                'size':size, 'type':f.get('type') or '',
                'created_at':f.get('created_at') or '', 'last_modified':str(f.get('last_modified') or ''),
                'ordinal':index, 'destination':str(dest), 'file_status':file_status,
                'actual_size':actual, 'sha256':sha256_file(dest) if actual == size else '',
            })
    return entries


def audit(entries):
    # Full metadata fingerprint. Identical data suggests common upload/provenance,
    # not mathematical proof of matching file content across distinct file_ids.
    by_metadata = defaultdict(list)
    for e in entries:
        k = (e['name'], e['size'], e['type'], e['created_at'], e['last_modified'])
        by_metadata[k].append(e)
    groups = [items for items in by_metadata.values() if len({x['gpt_id'] for x in items}) >= 2]
    groups.sort(key=lambda items: (-len(items), items[0]['name'], items[0]['gpt_id']))
    result_groups=[]
    result_members=[]
    possible_donor=[]
    group_id_by_member={}
    for number, items in enumerate(groups,1):
        group_id=f'SHARED-{number:04d}'
        local = [x for x in items if x['file_status']=='SIZE_MATCH_PRESENT']
        sha_set={x['sha256'] for x in local}
        evidence='METADATA_ONLY_NOT_BYTE_VERIFIED'
        if len(local)>=2:
            evidence = 'BYTE_HASH_CONFLICT' if len(sha_set)>1 else 'PRESENT_MEMBERS_SHA256_MATCH'
        result_groups.append({
            'group_id':group_id, 'name':items[0]['name'], 'size':items[0]['size'],
            'type':items[0]['type'], 'created_at':items[0]['created_at'],
            'last_modified':items[0]['last_modified'],
            'member_entries':len(items), 'member_gpts':len({x['gpt_id'] for x in items}),
            'present_exact_size':len(local), 'missing':sum(x['file_status']=='MISSING' for x in items),
            'present_hashes':len(sha_set), 'evidence':evidence,
            'note':'Совпадение метаданных НЕ доказывает равенство байтов для файлов, которые не были скачаны.'
        })
        for x in items:
            group_id_by_member[(x['gpt_id'],x['file_id'])]=group_id
            donors=[y for y in local if y['gpt_id'] != x['gpt_id']]
            # Hash ambiguity among available donors: do NOT recommend a single source.
            donors_valid = len({d['sha256'] for d in donors})<=1
            donor=donors[0] if donors and donors_valid else None
            copy_candidate_status=('CANDIDATE_ONLY_NOT_CONFIRMED' if x['file_status']=='MISSING' and donor
                                   else ('DONOR_CONFLICT' if x['file_status']=='MISSING' and donors and not donors_valid
                                         else ''))
            result_members.append({
                'group_id':group_id, **x,
                'donor_gpt':donor['gpt_name'] if donor else '',
                'donor_gpt_id':donor['gpt_id'] if donor else '',
                'donor_file_id':donor['file_id'] if donor else '',
                'donor_sha256':donor['sha256'] if donor else '',
                'donor_path':donor['destination'] if donor else '',
                'candidate_status':copy_candidate_status,
                'verification':'UNVERIFIED_FOR_MISSING_TARGET' if x['file_status']=='MISSING' else 'LOCAL_BYTES_SHA256_ONLY',
            })
            if copy_candidate_status:
                possible_donor.append(result_members[-1])
    return result_groups, result_members, possible_donor


def main():
    ap=argparse.ArgumentParser(description='Без сети: поиск общих источников Knowledge у 250 GPT. Никакого копирования.')
    ap.add_argument('--snapshot', type=Path, default=Path('Custom_GPT_backup/gpt_list_snapshot.json'))
    ap.add_argument('--backup', type=Path, default=Path('Custom_GPT_backup'))
    ap.add_argument('--out', type=Path, default=None,
                    help='Папка для CSV. По умолчанию Custom_GPT_backup')
    args=ap.parse_args()
    if not args.snapshot.is_file():
        ap.error('snapshot не найден: '+str(args.snapshot))
    out=args.out or args.backup
    entries=load_entries(args.snapshot,args.backup)
    groups,members,candidates=audit(entries)
    group_fields=['group_id','name','size','type','created_at','last_modified','member_entries','member_gpts',
                  'present_exact_size','missing','present_hashes','evidence','note']
    member_fields=['group_id','gpt_name','gpt_id','name','file_id','size','type','created_at','last_modified',
                   'ordinal','destination','file_status','actual_size','sha256','donor_gpt','donor_gpt_id',
                   'donor_file_id','donor_sha256','donor_path','candidate_status','verification']
    p1=out/'shared_knowledge_metadata_groups.csv'
    p2=out/'shared_knowledge_members.csv'
    p3=out/'shared_knowledge_possible_donors.csv'
    write_csv(p1,group_fields,groups)
    write_csv(p2,member_fields,members)
    write_csv(p3,member_fields,candidates)
    print('=== ОФЛАЙН-АУДИТ ОБЩИХ ИСТОЧНИКОВ KNOWLEDGE ===')
    print('Всего Knowledge-записей:',len(entries))
    print('Групп полного совпадения метаданных в нескольких GPT:',len(groups))
    print('Knowledge-записей в этих группах:',len(members))
    print('Записей, для которых есть кандидат-донор на диске:',sum(x['candidate_status']=='CANDIDATE_ONLY_NOT_CONFIRMED' for x in candidates))
    print('Групп с конфликтом SHA-256 среди скачанных экземпляров:',sum(x['evidence']=='BYTE_HASH_CONFLICT' for x in groups))
    print('Ни один кандидат НЕ объявлен подтверждённым оригиналом целевого GPT.')
    print('Данные не копируются; только создаются CSV.')
    print('Группы:',p1)
    print('Участники:',p2)
    print('Кандидаты-доноры:',p3)


if __name__=='__main__':main()
