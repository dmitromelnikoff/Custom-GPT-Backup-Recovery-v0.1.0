#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Резервные копии иконок СОБСТВЕННЫХ Custom GPT по gpt_list_snapshot.json.

Скачивает аватар/профильную картинку GPT в .../gpts/<name>/<g-id>/icon.ext.
Это НЕ изображения, добавленные в Knowledge.

Только чтение ChatGPT. Не меняет GPT, Knowledge, Obsidian и существующие icon.*.
Не записывает токены, cookie, подписанные ссылки в файлы или консоль.
Нужны: pip install playwright psutil. Chrome уже открыт в CDP-профиле.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

BASE = 'https://chatgpt.com'
EDITOR = BASE + '/gpts/editor/{}'
MAX_ICON_BYTES = 12 * 1024 * 1024
IMAGE_EXT = {'png','jpg','gif','webp','bmp','ico','avif'}
STOP_HTTP = {401, 403, 429}
COLUMNS = ['gpt_id', 'name', 'pic_id', 'status', 'icon_file', 'bytes', 'sha256', 'source', 'error', 'checked_utc']

# Токен используется только в памяти для запросов в chatgpt.com, никогда в отчётах.
SESSION_JS = """async () => {
  try {
    const r = await fetch('/api/auth/session', {credentials:'include',cache:'no-store'});
    if (!r.ok) return null;
    const j = await r.json();
    return j.accessToken || j.access_token || null;
  } catch(e) { return null; }
}"""


def clean(value, maxlen=90):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:maxlen].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON','PRN','AUX','NUL','COM1','LPT1'}:
        s = '_' + s
    return s


def classify_image(data:bytes):
    if len(data) < 16 or len(data) > MAX_ICON_BYTES:
        return None
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png' if data[12:16] == b'IHDR' else None
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg' if data.endswith(b'\xff\xd9') else None
    if data.startswith((b'GIF87a',b'GIF89a')):
        return 'gif' if data[-1:] == b';' else None
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    if data.startswith(b'BM'):
        return 'bmp'
    if data.startswith(b'\x00\x00\x01\x00'):
        return 'ico'
    if len(data) >= 16 and data[4:12] in (b'ftypavif',b'ftypavis'):
        return 'avif'
    return None


def read_gpts(path:Path):
    blob = json.loads(path.read_text(encoding='utf-8-sig'))
    entries = blob.get('gpts', []) if isinstance(blob,dict) else blob
    if not isinstance(entries,list):
        raise ValueError('Неверная структура snapshot: массив gpts отсутствует')
    out=[]; seen=set()
    for entry in entries:
        res = entry.get('resource',entry)
        g = res.get('gizmo',res)
        gid = g.get('id','')
        if not isinstance(gid,str) or not gid.startswith('g-') or gid.startswith('g-p-'):
            continue
        if gid in seen:
            raise ValueError('Повторяющийся GPT ID: '+gid)
        seen.add(gid)
        display = g.get('display') or {}
        out.append({'gpt_id':gid,'name':str(display.get('name') or gid),
                    'pic_id':str(display.get('profile_pic_id') or ''),
                    'pic_url':str(display.get('profile_picture_url') or '')})
    return out


def sha(data:bytes):
    return hashlib.sha256(data).hexdigest()


def image_location(backup:Path,g:dict):
    return backup/'gpts'/clean(g['name'],68)/clean(g['gpt_id'],90)


def existing_images(folder:Path):
    found=[]; invalid=[]
    for p in sorted(folder.glob('icon.*')):
        if not p.is_file() or p.suffix[1:].lower() not in IMAGE_EXT:
            continue
        if p.stat().st_size > MAX_ICON_BYTES:
            invalid.append(p);continue
        b=p.read_bytes()
        if classify_image(b) == p.suffix[1:].lower():
            found.append((p,len(b),sha(b)))
        else:
            invalid.append(p)
    return found,invalid


def valid_url(raw):
    if not isinstance(raw,str) or not raw:
        return False
    p=urlsplit(raw)
    if p.scheme!='https' or not p.hostname or p.username or p.password:
        return False
    # URL должен принадлежать инфраструктуре, которой пользуется UI иконок.
    host=p.hostname.lower()
    return (host=='chatgpt.com' or host.endswith('.chatgpt.com') or
            host=='oaistatic.com' or host.endswith('.oaistatic.com') or
            host=='openai.com' or host.endswith('.openai.com'))


def _is_auth_host(url):
    return urlsplit(url).hostname == 'chatgpt.com'


def fetch_image(ctx,url,token):
    """Возвращает (body, status, reason). Redirect не переносит Bearer на другие домены."""
    if not valid_url(url):
        return None, 0, 'Неподдерживаемый адрес изображения'
    current=url
    for _ in range(5):
        headers={'Accept':'image/avif,image/webp,image/png,image/jpeg,image/gif,image/*'}
        if token and _is_auth_host(current):
            headers['Authorization']='Bearer '+token
        try:
            r=ctx.request.get(current,timeout=30000,max_redirects=0,headers=headers)
        except Exception as exc:
            return None,0,type(exc).__name__
        code=r.status
        if code in (301,302,303,307,308):
            location=r.headers.get('location')
            if not location:
                return None,code,'Redirect без Location'
            current=urljoin(current,location)
            if not valid_url(current):
                return None,code,'Redirect за пределы разрешённых хостов'
            continue
        if code!=200:
            return None,code,f'HTTP {code}'
        size=r.headers.get('content-length','')
        if size.isdigit() and int(size)>MAX_ICON_BYTES:
            return None,code,'Превышен лимит размера иконки'
        body=r.body()
        if len(body)>MAX_ICON_BYTES:
            return None,code,'Превышен лимит размера иконки'
        if classify_image(body) is None:
            return None,code,'Ответ не является валидным изображением'
        return body,code,'ok'
    return None,0,'Слишком много redirect'


def ui_url_candidates(page,pic_id):
    """Только адреса реально отображённых изображений с ТОЧНЫМ pic_id."""
    if not pic_id:
        return []
    return page.evaluate("""(id) => {
       const found=[];
       const add = (v) => {
         if (typeof v === 'string' && v.includes(id) && v.startsWith('https://')) found.push(v);
       };
       for(const el of document.querySelectorAll('img')) {
         add(el.currentSrc); add(el.src);
         const srcset=el.getAttribute('srcset') || '';
         for(const entry of srcset.split(',')) add(entry.trim().split(/\\s+/)[0]);
       }
       for(const el of document.querySelectorAll('[style*="background"]')) {
         const bg=getComputedStyle(el).backgroundImage || '';
         for(const match of bg.matchAll(/url\\([\\"']?([^\\)\\"']+)[\\"']?\\)/g)) add(match[1]);
       }
       return [...new Set(found)];
    }""", pic_id)


def refresh_url_via_editor(page,g):
    """Открывает видимый редактор своего GPT и ищет URL картинки в DOM."""
    try:
        response=page.goto(EDITOR.format(g['gpt_id']),wait_until='domcontentloaded',timeout=60000)
        if response and response.status in STOP_HTTP:
            return [],response.status,'Редактор вернул HTTP '+str(response.status)
        page.wait_for_timeout(1700)
        urls=ui_url_candidates(page,g['pic_id'])
        if urls:
            return [u for u in urls if valid_url(u)],0,'UI'
        # У части аккаунтов иконка показана только на вкладке «Конфигурация».
        for role in ('radio','tab'):
            try:
                loc=page.get_by_role(role,name=re.compile('^(Конфигурация|Configure)$',re.I))
                if loc.count()!=1 or not loc.is_visible():
                    continue
                if loc.get_attribute('aria-checked') != 'true' and loc.get_attribute('aria-selected') != 'true':
                    loc.click(timeout=4000)
                page.wait_for_timeout(1200)
                urls=ui_url_candidates(page,g['pic_id'])
                if urls:
                    return [u for u in urls if valid_url(u)],0,'UI configuration'
            except Exception:
                pass
        return [],0,'Не найден URL иконки с ожидаемым file_id в UI'
    except Exception as exc:
        return [],0,'Ошибка UI: '+type(exc).__name__


def _debug_port_works(port):
    if not 1 <= int(port) <= 65535:
        return False
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:'+str(port)+'/json/version',timeout=2) as response:
            j=json.load(response)
        ws=str(j.get('webSocketDebuggerUrl',''))
        return ws.startswith(('ws://127.0.0.1:','ws://localhost:'))
    except Exception:
        return False


def cdp_port(profile, port):
    if port is not None:
        if not _debug_port_works(port):
            raise RuntimeError('CDP порт не является рабочим Chrome на 127.0.0.1')
        return port
    p=profile/'DevToolsActivePort'
    if p.is_file():
        try:
            port=int(p.read_text(encoding='utf-8').splitlines()[0])
            if _debug_port_works(port):return port
        except Exception:pass
    try:
        import psutil
    except ImportError as exc:
        raise RuntimeError('Для --attach требуется pip install psutil') from exc
    wanted=os.path.normcase(os.path.abspath(str(profile)))
    for proc in psutil.process_iter(attrs=['cmdline']):
        try:
            argv=proc.info.get('cmdline') or []
            def arg_of(key):
                for i,a in enumerate(argv):
                    if a.startswith(key+'='):return a.split('=',1)[1]
                    if a==key and i+1<len(argv):return argv[i+1]
                return None
            pr=arg_of('--user-data-dir')
            po=arg_of('--remote-debugging-port')
            if pr and po and os.path.normcase(os.path.abspath(pr.strip('"')))==wanted:
                n=int(po)
                if _debug_port_works(n):return n
        except (ValueError,OSError,psutil.NoSuchProcess,psutil.AccessDenied):pass
    raise RuntimeError('Не найден открытый Chrome с CDP и профилем '+str(profile))


def save_results(backup,records):
    backup.mkdir(parents=True,exist_ok=True)
    report=backup/'gpt_icons_inventory_v2.json'
    previous={}
    if report.is_file():
        try:
            for row in json.loads(report.read_text(encoding='utf-8')).get('gpts',[]):
                previous[row['gpt_id']]=row
        except Exception:
            pass
    for row in records:
        previous[row['gpt_id']]=row
    data={'version':2,'updated_utc':datetime.now(timezone.utc).isoformat(),
          'gpts':[previous[k] for k in sorted(previous)]}
    tmp=report.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(tmp,report)
    csvpath=backup/'gpt_icons_inventory_v2.csv'
    tmp=csvpath.with_suffix('.csv.tmp')
    with tmp.open('w',encoding='utf-8-sig',newline='') as fh:
        writer=csv.DictWriter(fh,fieldnames=COLUMNS,delimiter=';',extrasaction='ignore')
        writer.writeheader();writer.writerows(data['gpts'])
    os.replace(tmp,csvpath)
    return csvpath


def install_image(folder,body):
    ext=classify_image(body)
    if not ext:
        raise ValueError('Неподдерживаемый формат изображения')
    existing,invalid=existing_images(folder)
    if existing:
        return existing[0][0],False
    if invalid:
        raise ValueError('В папке уже есть icon.* с некорректным форматом; не перезаписываю')
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/('icon.'+ext)
    if target.exists():
        raise FileExistsError('Иконка уже существует')
    part=folder/(target.name+'.'+str(os.getpid())+'.part')
    try:
        with part.open('xb') as f:
            f.write(body)
            f.flush();os.fsync(f.fileno())
        # OS atomic hardlink prevents accidental overwrite on concurrent launch.
        os.link(part,target)
    finally:
        if part.exists():part.unlink()
    return target,True


def main(argv=None):
    parser=argparse.ArgumentParser(description='Восстановление иконок собственных Custom GPT (не Knowledge-картинок)')
    parser.add_argument('--plan-only',action='store_true',help='Только офлайн-проверка, без Chrome')
    parser.add_argument('--attach',action='store_true',help='Подключиться к авторизованному Chrome через CDP')
    parser.add_argument('--cdp-port',type=int)
    parser.add_argument('--profile',default='.chatgpt_backup_normal_chrome_profile')
    parser.add_argument('--snapshot',default=None)
    parser.add_argument('--out',default='Custom_GPT_backup')
    parser.add_argument('--ids',nargs='+')
    parser.add_argument('--limit',type=int,default=0,help='Проверить максимум N GPT')
    parser.add_argument('--pause-min',type=float,default=15)
    parser.add_argument('--pause-max',type=float,default=30)
    parser.add_argument('--max-consecutive-failures',type=int,default=8)
    args=parser.parse_args(argv)
    if not args.plan_only and not args.attach:
        parser.error('Для работы с сетью укажи --attach; для офлайн-проверки --plan-only')
    if args.pause_min < 0 or args.pause_max < args.pause_min:
        parser.error('Требуется 0 <= --pause-min <= --pause-max')
    backup=Path(args.out).expanduser().resolve()
    candidates=[Path(args.snapshot)] if args.snapshot else [backup/'gpt_list_snapshot.json',Path('gpt_list_snapshot.json')]
    snap=next((p for p in candidates if p.is_file()),None)
    if not snap:
        parser.error('Не найден gpt_list_snapshot.json')
    gpts=read_gpts(snap)
    if args.ids:
        wanted=set(args.ids)
        gpts=[g for g in gpts if g['gpt_id'] in wanted]
        missing=wanted-{g['gpt_id'] for g in gpts}
        if missing:print('В snapshot нет ID: '+', '.join(sorted(missing)))
    if args.limit>0:gpts=gpts[:args.limit]
    with_icon=sum(bool(g['pic_id'] or g['pic_url']) for g in gpts)
    existing=sum(bool(existing_images(image_location(backup,g))[0]) for g in gpts)
    print(f'GPT в очереди: {len(gpts)}; с указанной иконкой: {with_icon}; уже есть icon.*: {existing}')
    print('Это профильные ИКОНКИ GPT, НЕ PNG/JPG в Knowledge. Knowledge и Obsidian НЕ изменяются.')
    if args.plan_only:
        print('РЕЖИМ ПРОВЕРКИ: запросов к ChatGPT нет; сохранённых файлов не менял.')
        return 0
    from playwright.sync_api import sync_playwright
    profile=Path(args.profile).expanduser().resolve()
    port=cdp_port(profile,args.cdp_port)
    results=[]; consecutive=0; aborted=False
    with sync_playwright() as play:
        browser=play.chromium.connect_over_cdp('http://127.0.0.1:'+str(port),timeout=20000)
        if not browser.contexts:
            raise RuntimeError('У подключённого Chrome нет браузерного контекста')
        ctx=browser.contexts[0]
        page=ctx.new_page()
        try:
            initial=page.goto(BASE+'/gpts/mine',wait_until='domcontentloaded',timeout=60000)
            if initial and initial.status in STOP_HTTP:
                raise RuntimeError('Нет обычного доступа к ChatGPT (HTTP '+str(initial.status)+'). Сетевые попытки прекращены.')
            token=page.evaluate(SESSION_JS)
            for i,g in enumerate(gpts,1):
                folder=image_location(backup,g)
                row={'gpt_id':g['gpt_id'],'name':g['name'],'pic_id':g['pic_id'],
                     'status':'','icon_file':'','bytes':0,'sha256':'','source':'','error':'',
                     'checked_utc':datetime.now(timezone.utc).isoformat()}
                present,invalid=existing_images(folder)
                if present:
                    f,n,h=present[0]
                    row.update(status='verified_existing_format',icon_file=f.name,bytes=n,sha256=h,source='existing')
                    consecutive=0
                elif invalid:
                    row.update(status='CONFLICT_EXISTING_ICON',error='В папке обнаружен некорректный icon.*; ручная проверка')
                    consecutive+=1
                elif not(g['pic_id'] or g['pic_url']):
                    row.update(status='not_set')
                    consecutive=0
                else:
                    urls=[(g['pic_url'],'snapshot')] if valid_url(g['pic_url']) else []
                    got=None; source=''; last_http=0; reason=''
                    for url,origin in urls:
                        got,last_http,reason=fetch_image(ctx,url,token)
                        if got:source=origin;break
                        if last_http in (401,429):
                            break
                    if not got and last_http not in (401,429):
                        refreshed,ui_http,why=refresh_url_via_editor(page,g)
                        if ui_http in STOP_HTTP:
                            last_http=ui_http;reason=why
                        else:
                            # Обновить токен только для chatgpt.com. Не выводить и не сохранять.
                            try:token=page.evaluate(SESSION_JS) or token
                            except Exception:pass
                            for url in refreshed:
                                if url==g['pic_url']:continue
                                got,last_http,reason=fetch_image(ctx,url,token)
                                if got:source='editor_visible_ui';break
                                if last_http in STOP_HTTP:break
                            if not got and not reason:reason=why
                    if got:
                        try:
                            f,is_new=install_image(folder,got)
                            row.update(status='downloaded' if is_new else 'verified_existing_format',
                                       icon_file=f.name,bytes=len(got),sha256=sha(got),source=source)
                            consecutive=0
                        except Exception as exc:
                            row.update(status='SAVE_ERROR',error=type(exc).__name__+': '+str(exc)[:100]);consecutive+=1
                    else:
                        row.update(status='HTTP_ACCESS_STOP' if last_http in STOP_HTTP else 'not_downloaded',
                                   error=('HTTP '+str(last_http)+'; '+reason)[:140])
                        consecutive+=1
                        if last_http in STOP_HTTP:aborted=True
                results.append(row)
                report=save_results(backup,results)
                print(f'[{i}/{len(gpts)}] {g["name"]}: {row["status"]} {row["error"]}')
                if aborted:
                    print('ОСТАНОВКА по HTTP 401/403/429. Без автоматических повторов.')
                    break
                if consecutive>=args.max_consecutive_failures>0:
                    print('ОСТАНОВКА: слишком много неудачных GPT подряд.')
                    break
                if i<len(gpts):time.sleep(random.uniform(args.pause_min,args.pause_max))
        finally:
            try:page.close()
            except Exception:pass
            browser.close()
    print('Иконок в этом запуске получено:',sum(r['status']=='downloaded' for r in results))
    print('Журнал:',backup/'gpt_icons_inventory_v2.csv')
    return 0 if not aborted else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit('Остановлено по Ctrl+C. Скачанные иконки сохранены.')
