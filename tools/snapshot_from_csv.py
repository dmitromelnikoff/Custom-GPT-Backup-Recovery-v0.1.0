#!/usr/bin/env python3
"""Offline conversion of an owner-provided inventory CSV to private GPT snapshot.

Does not discover any GPTs, query ChatGPT or invent file IDs/sizes. The caller is
responsible for capturing accurate metadata from their own authorized sources.
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

REQUIRED = ('gpt_id','gpt_name','file_name','file_id','expected_bytes','ordinal')


def convert(source: Path, output: Path):
    with source.open('r', encoding='utf-8-sig', newline='') as f:
        reader=csv.DictReader(f, delimiter=';')
        if not reader.fieldnames or any(k not in reader.fieldnames for k in REQUIRED):
            raise ValueError('Required CSV columns: ' + ', '.join(REQUIRED))
        rows=list(reader)
    if not rows: raise ValueError('CSV empty')
    groups=OrderedDict()
    for i, row in enumerate(rows, start=2):
        gid, gname = row['gpt_id'].strip(), row['gpt_name'].strip()
        if not gid.startswith('g-') or not gname: raise ValueError(f'Row {i}: invalid gpt_id/name')
        group=groups.setdefault(gid, {'name': gname, 'records': [], 'picture': '', 'picture_url':''})
        if group['name']!=gname: raise ValueError(f'Row {i}: inconsistent name for {gid}')
        pic=(row.get('pic_id') or '').strip()
        pic_url=(row.get('pic_url') or '').strip()
        if pic:group['picture']=pic
        if pic_url:group['picture_url']=pic_url
        fn=(row['file_name'] or '').strip()
        if not fn:
            if any((row[x] or '').strip() for x in ('file_id','expected_bytes','ordinal')):
                raise ValueError(f'Row {i}: incomplete empty file entry')
            continue
        fid=(row['file_id'] or '').strip()
        if not fid: raise ValueError(f'Row {i}: missing file_id')
        try:
            size=int(row['expected_bytes'])
            ordinal=int(row['ordinal'])
        except (ValueError,TypeError) as exc:raise ValueError(f'Row {i}: invalid size/ordinal') from exc
        if not (0<size<512*1024*1024 and ordinal>=1):
            raise ValueError(f'Row {i}: invalid size/ordinal range')
        group['records'].append((ordinal, {'file_id':fid,'name':fn,'size':size,
            'type':row.get('type') or '', 'created_at':row.get('created_at') or '',
            'last_modified':row.get('last_modified') or ''}))
    gpts=[]
    for gid,g in groups.items():
        vals=sorted(g['records'], key=lambda x:x[0])
        ordinals=[x[0] for x in vals]
        if ordinals!=list(range(1,len(vals)+1)):
            raise ValueError(f'{gid}: ordinals must be exactly 1..N, found {ordinals}')
        file_ids=[x[1]['file_id'] for x in vals]
        if len(set(file_ids))!=len(file_ids):
            raise ValueError(f'{gid}: duplicate file_id within GPT')
        gpts.append({'resource':{'gizmo':{'id':gid,'display':{
            'name':g['name'],'profile_pic_id':g['picture'] or None,
            'profile_picture_url':g['picture_url'] or None}},
            'files':[obj for _,obj in vals]}})
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():raise FileExistsError(f'Refusing to overwrite snapshot: {output}')
    with output.open('x',encoding='utf-8') as f:
        json.dump({'created_utc':datetime.now(timezone.utc).isoformat(),
                   'source':'owner_supplied_csv_not_verified_by_chatgpt',
                   'count':len(gpts),'gpts':gpts},f,indent=2,ensure_ascii=False)
    return len(gpts),sum(len(g['resource']['files']) for g in gpts)


def main(argv=None):
    ap=argparse.ArgumentParser(description='Build private snapshot from owner-provided GPT inventory CSV, offline')
    ap.add_argument('--csv',required=True)
    ap.add_argument('--out',default='Custom_GPT_backup/gpt_list_snapshot.json')
    args=ap.parse_args(argv)
    n,m=convert(Path(args.csv),Path(args.out))
    print(f'GPT: {n}; Knowledge files: {m}; private snapshot: {args.out}')
    return 0


if __name__=='__main__':
    sys.exit(main())
