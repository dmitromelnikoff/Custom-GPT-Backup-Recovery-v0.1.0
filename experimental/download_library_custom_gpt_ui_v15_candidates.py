# WARNING: THIS TOOL STAGES LIBRARY CANDIDATES ONLY, NEVER VERIFIED GPT ORIGINALS.
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Download own Custom-GPT Knowledge from the *visible ChatGPT Library UI*.

No undocumented ChatGPT API, auth token extraction, plugins, or GPT editing.
V14: keep size-mismatched downloads in a separate local quarantine, record actual
size and SHA256, and classify HTML/error responses before checking Knowledge size.
V13: add one safe alternate download route through the filename row-actions menu
when a document preview download produces no Chrome download event.
V12: add 15–30s variable normal pauses while preserving v11 scoped limits.
V11: scope network access-denial signals to the temporary Library tab
and its own popups. Other ChatGPT tabs never set the Library 429 state.
Respect Library HTTP 429, Retry-After and visible account access warnings.
The source remains original Custom GPT Knowledge from Library only.
Attaches to an EXISTING Chrome launched with loopback DevTools by prior scripts.
Searches Library by filename and uses the normal visible Download control.

Important: identical filenames can refer to different files/versions. This tool
will not silently assign ambiguous downloads to the wrong GPT. Every result is
logged; unknown UI layout stops safely with local-only diagnostics.

Requires: pip install playwright psutil ; python -m playwright install chromium
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import math
import random
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import unicodedata
import urllib.request
from pathlib import Path

BASE = 'https://chatgpt.com/library'
MAX_BYTES = 512 * 1024 * 1024
ACCESS_DENIAL = {401, 403, 429}
MAX_AUTOMATIC_COOLDOWN = 3600  # Hard ceiling: do not override server limits with shorter waits.


def retry_after_seconds(value, now=None):
    """Read a server Retry-After (seconds or HTTP date), or None if unknown."""
    value=str(value or '').strip()
    if not value:
        return None
    try:
        seconds=float(value)
        if 0 <= seconds < 1e12:
            return seconds
    except (ValueError, OverflowError):
        pass
    try:
        at=parsedate_to_datetime(value)
        if at.tzinfo is None:
            at=at.replace(tzinfo=timezone.utc)
        now=now or datetime.now(timezone.utc)
        return max(0.0, (at-now).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def cooldown_seconds(requested, server_retry_after):
    """Never resume earlier than requested cooldown or a valid Retry-After."""
    return max(float(requested), float(server_retry_after or 0.0))


def log_rate_limit(backup, filename, outcome, requested_wait=0, server_retry_after=None):
    """Append a local, token-free audit note for an HTTP 429 decision."""
    path=backup/'library_v9_rate_limit_events.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    keys=('timestamp_utc','file_name','outcome','cooldown_seconds','server_retry_after_seconds')
    with path.open('a',encoding='utf-8-sig' if not path.exists() else 'utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=keys,delimiter=';')
        if stream.tell()==0:
            writer.writeheader()
        writer.writerow({
            'timestamp_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'file_name':filename,
            'outcome':outcome,
            'cooldown_seconds':round(float(requested_wait),1),
            'server_retry_after_seconds':('' if server_retry_after is None else round(float(server_retry_after),1)),
        })


def safe_part(v, limit=100):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(v))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        s = '_' + s
    return s


def norm(s):
    return unicodedata.normalize('NFC', str(s)).casefold().strip()


def parse_snapshot(src, backup):
    blob = json.loads(src.read_text('utf-8-sig'))
    items = blob.get('gpts', []) if isinstance(blob, dict) else blob
    result=[]
    for entry in items:
        resource = entry.get('resource', entry)
        g = resource.get('gizmo', resource)
        gid = g.get('id', '')
        if not (isinstance(gid,str) and gid.startswith('g-') and not gid.startswith('g-p-')):
            continue
        name = (g.get('display') or {}).get('name') or gid
        dest_dir=backup/'gpts'/safe_part(name,68)/safe_part(gid,90)/'knowledge'
        for pos, item in enumerate(resource.get('files') or [], 1):
            filename=item.get('name')
            size=item.get('size')
            if not isinstance(filename,str) or not isinstance(size,int) or size<1 or size>MAX_BYTES:
                continue
            result.append({'gpt_id':gid, 'gpt_name':name, 'name':filename,
                           'size':size, 'file_id':item.get('file_id') or '',
                           'dest':dest_dir/f'{pos:02d}_{safe_part(filename)}'})
    return result


def maybe_valid(path, expected):
    return path.is_file() and path.stat().st_size == expected


def load_validated_queue(queue_file, all_targets):
    """Accept ONLY records matching the owned GPT snapshot; ignore CSV local paths.

    The queue may come from an older Windows machine. Never trust the CSV's
    local_path for writing, even when it looks plausible.
    """
    by_key = {(t['gpt_id'], t['file_id'], norm(t['name']), t['size']): t
              for t in all_targets}
    global_names = collections.defaultdict(set)
    for item in all_targets:
        global_names[norm(item['name'])].add(item['file_id'])
    selected = []
    seen = set()
    if not queue_file.is_file():
        raise ValueError(f'Не найден CSV очереди: {queue_file}')
    with queue_file.open('r', encoding='utf-8-sig', newline='') as f:
        r = csv.DictReader(f, delimiter=';')
        expected = {'gpt_id', 'file_id', 'file_name', 'expected_bytes'}
        if not expected.issubset(set(r.fieldnames or [])):
            raise ValueError(f'Неверные столбцы CSV: нужны {sorted(expected)}')
        for line_no, row in enumerate(r, start=2):
            try:
                key = (row['gpt_id'], row['file_id'], norm(row['file_name']),
                       int(row['expected_bytes']))
            except (ValueError, TypeError, KeyError) as exc:
                raise ValueError(f'Ошибка в строке очереди {line_no}') from exc
            if key not in by_key:
                raise ValueError(f'Строка {line_no} не совпадает с snapshot (GPT, file_id, имя, размер)')
            if key in seen:
                raise ValueError(f'Дублирующаяся строка очереди {line_no}')
            if len(global_names[key[2]]) != 1:
                raise ValueError(f'Строка {line_no} неоднозначна: это имя имеет разные file_id в GPT. '
                                 'Для неё нужна отдельная процедура сверки, а не массовая очередь.')
            seen.add(key)
            selected.append(by_key[key])
    return selected


def read_queue_progress(path):
    """Read prior v7 statuses; never assume a prior success if bytes are absent."""
    states = {}
    if not path.exists():
        return states
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        for r in csv.DictReader(f, delimiter=';'):
            try:
                key = (r['gpt_id'], r['file_id'], norm(r['file_name']), int(r['expected_bytes']))
                states[key] = {'status':r.get('status',''), 'note':r.get('note',''),
                               'sha256':r.get('sha256','')}
            except (KeyError, TypeError, ValueError):
                continue
    return states


def init_queue_status(selected, previous, retry_recorded=False, retry_ui_mismatch=False,
                      retry_wrong_size=False):
    """Resume by exact identity. Failed searches never imply source deletion."""
    totals=collections.Counter()
    for t in selected:
        key=(t['gpt_id'], t['file_id'], norm(t['name']), t['size'])
        old=previous.get(key, {})
        dest=t['dest']
        if maybe_valid(dest, t['size']):
            t['status']='ALREADY_EXISTS'
            t['note']='Локальный файл совпадает по размеру с snapshot (происхождение определяется отдельно)'
        elif (old.get('status') == 'LIBRARY_CANDIDATE_STAGED'
              and candidate_present(backup=dest.parents[4], target=t, digest=old.get('sha256',''))):
            t['status']='LIBRARY_CANDIDATE_STAGED'
            t['sha256']=old.get('sha256','')
            t['note']='Отдельный кандидат из Library существует, проверены размер и SHA-256; НЕ оригинал GPT'
        elif dest.exists():
            t['status']='LOCAL_SIZE_MISMATCH'
            t['note']='Локальный файл с неправильным размером; не перезаписывать автоматически'
        elif old.get('status') == 'LIBRARY_CANDIDATE_STAGED':
            t['status']='PENDING'
            t['note']='Кандидат из Library ранее был получен, но теперь отсутствует или не прошёл SHA-256; требуется проверка'
        elif old.get('status') in ('DOWNLOADED','ALREADY_EXISTS'):
            t['status']='PENDING'
            t['note']='Ранее файл отмечался как сохранённый, но сейчас отсутствует на диске'
        elif retry_wrong_size and old.get('status') in ('WRONG_SIZE','ERROR_RESPONSE_NOT_FILE'):
            t['status']='PENDING'
            t['note']='Повтор по явному запросу после несоответствия скачанного содержимого'
        elif (retry_ui_mismatch and old.get('status') in (
                'DOWNLOAD_BUTTON_NOT_FOUND', 'NO_BROWSER_DOWNLOAD_EVENT',
                'DOWNLOAD_SUBMENU_UNRECOGNIZED', 'NO_BROWSER_DOWNLOAD_EVENT_AFTER_MENU',
                'AMBIGUOUS_BROWSER_DOWNLOAD_EVENTS', 'BROWSER_DOWNLOAD_FAILED')
                or retry_ui_mismatch and old.get('status','').startswith('ROW_FALLBACK_')
                or retry_ui_mismatch and old.get('status','').startswith('UI_ERROR_')):
            t['status']='PENDING'
            t['note']='Повтор после ошибки интерфейса: '+old.get('status','')
        elif old and old.get('status') not in ('PENDING','', 'NOT_ATTEMPTED') and not retry_recorded:
            t['status']=old['status']
            t['note']=old.get('note','')
            t['sha256']=old.get('sha256','')
        else:
            t['status']='PENDING'
            t['note']=''
        totals[t['status']]+=1
    return totals


def offline_queue_preview(backup, selected):
    """Write a separate queue preview; do not touch existing download inventories."""
    out=backup/'library_phase2_queue_preview.csv'
    report(out, selected)
    counts=collections.Counter(t['status'] for t in selected)
    print('ОФЛАЙН-ПРОВЕРКА ОЧЕРЕДИ. Chrome и сеть не используются.')
    print('Строк в проверенной очереди:',len(selected))
    for k,v in sorted(counts.items()):
        print(f'  {k}: {v}')
    if counts.get('WRONG_SIZE',0):
        print('Несовпадение размера (не считается оригиналом):')
        for t in selected:
            if t['status']=='WRONG_SIZE':
                print('  ', t['gpt_name'], '/', t['name'], '| ожидалось:', t['size'])
                if t.get('note'):print('     ',t['note'])
    print('CSV для проверки:',out)
    return 0


def existing_chrome_port(profile: Path, explicit_port):
    """Connect ONLY to loopback devtools of a Chrome with the expected profile."""
    import psutil
    def works(port):
        if not 1<=port<=65535:return False
        try:
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with op.open(f'http://127.0.0.1:{port}/json/version',timeout=2) as response:
                data=json.load(response)
            return ('Chrome/' in data.get('Browser','') or 'Chromium/' in data.get('Browser',''))
        except Exception: return False
    if explicit_port is not None:
        if works(explicit_port):return explicit_port
        raise RuntimeError('CDP-порт не отвечает как локальный Chrome. Сессию не изменяли.')
    # Original exporter uses a unique profile and --remote-debugging-port.
    target=os.path.normcase(os.path.abspath(str(profile)))
    portfile=profile/'DevToolsActivePort'
    if portfile.is_file():
        try:
            port=int(portfile.read_text('utf-8').splitlines()[0])
            if works(port):return port
        except Exception:pass
    for process in psutil.process_iter(attrs=['cmdline']):
        try:
            argv=process.info.get('cmdline') or []
            flags={}
            for i,a in enumerate(argv):
                for key in ('--remote-debugging-port','--user-data-dir'):
                    if a.startswith(key+'='):flags[key]=a.split('=',1)[1]
                    if a==key and i+1<len(argv):flags[key]=argv[i+1]
            port=int(flags.get('--remote-debugging-port') or 0)
            found=flags.get('--user-data-dir','')
            if found and os.path.normcase(os.path.abspath(found.strip('"'))) == target and works(port):
                return port
        except Exception:pass
    raise RuntimeError('Не найден уже открытый Chrome с DevTools для указанного --profile. '
                       'Обычный Chrome без отладки подключить нельзя. Не закрывай старое окно.')


def find_search_box(page):
    """Find Library search input, avoiding global sidebar search where possible."""
    css=[
        'main input[placeholder*="Поиск" i]', 'main input[placeholder*="Search" i]',
        'main input[type="search"]', 'main [role="searchbox"]',
        '[role="main"] input[placeholder*="Поиск" i]',
        '[role="main"] input[placeholder*="Search" i]',
        'input[placeholder*="Поиск" i]', 'input[placeholder*="Search files" i]',
        'input[placeholder*="Search library" i]',
        '[role="main"] input[type="text"]',
    ]
    for sel in css:
        try:
            matches=page.locator(sel).filter(visible=True)
            if matches.count()==1 and matches.first.is_editable(timeout=1500):return matches.first
        except Exception:pass
    return None



def wait_library_search(page, timeout_seconds=30):
    """Wait for the real Library search field after SPA navigation.

    DOMContentLoaded is not enough: Library can render a shell/sidebar before
    the search input appears. Never treat that transient state as file absence.
    """
    deadline=time.monotonic()+timeout_seconds
    while time.monotonic()<deadline:
        if is_challenge(page):
            return None, 'SECURITY_CHECK'
        try:
            box=find_search_box(page)
            if box is not None:
                return box, None
        except Exception:
            pass
        page.wait_for_timeout(500)
    return None, 'LIBRARY_SEARCH_TIMEOUT'


def previous_not_found(report_file, retry_not_found=False):
    """Only remember inconclusive Library UI misses, not failed downloads.

    Stored records are keyed by (GPT ID, Knowledge file ID, expected size).
    A later manual download can still be recognized by its actual file size.
    """
    saved=set()
    if retry_not_found or not report_file.is_file():
        return saved
    try:
        with report_file.open('r',encoding='utf-8-sig',newline='') as fh:
            for row in csv.DictReader(fh,delimiter=';'):
                if row.get('status') in ('NOT_FOUND','NOT_FOUND_IN_LIBRARY_SEARCH'):
                    try:
                        saved.add((row['gpt_id'],row['file_id'],int(row['expected_bytes'])))
                    except (KeyError,ValueError,TypeError):
                        pass
    except (OSError,UnicodeError,csv.Error):
        pass
    return saved


def ui_probe(page, out_dir, extra=''):
    """Minimal selector metadata; do NOT save page source, cookies, tokens, screenshots."""
    try:
        obj=page.evaluate('''() => ({
          url:location.origin+location.pathname,
          inputs:[...document.querySelectorAll('input')].filter(e=>e.getClientRects().length).map(e=>({
            placeholder:(e.placeholder||'').slice(0,90),aria:(e.getAttribute('aria-label')||'').slice(0,90),type:e.type
          })).slice(0,25),
          buttons:[...document.querySelectorAll('button')].filter(e=>e.getClientRects().length).map(e=>({
            label:(e.getAttribute('aria-label')||e.innerText||'').trim().slice(0,70)
          })).filter(e=>e.label).slice(0,45)
        })''')
    except Exception as exc:obj={'error':type(exc).__name__}
    obj['diagnostic_note']=extra[:160]
    out_dir.mkdir(parents=True,exist_ok=True)
    (out_dir/'library_ui_probe.json').write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Диагностика интерфейса (без токенов):',out_dir/'library_ui_probe.json')


def is_challenge(page):
    try:
        url=page.url.lower()
        if any(x in url for x in ('/auth/login','/login','/challenge','/cdn-cgi/')):return True
        content=page.locator('body').inner_text(timeout=1500).lower()[:20000]
        return any(x in content for x in ('verify you are human','confirm you are human',
                 'подтвердите, что вы человек','suspicious activity','подозрительная активность',
                 'unusual activity detected','слишком много запросов',
                 'вы отправляете запросы слишком часто',
                 'доступ к вашим диалогам временно ограничен',
                 'too many requests','temporarily restricted'))
    except Exception: return False


# No requests are made by these checks: they inspect visible browser UI only.
RESTRICTION_TEXTS = (
    'слишком много запросов', 'вы отправляете запросы слишком часто',
    'доступ к вашим диалогам временно ограничен',
    'too many requests', 'you are sending requests too frequently',
    'temporarily restricted', 'rate limit exceeded',
    'verify you are human', 'confirm you are human',
    'подтвердите, что вы человек',
)


def restriction_visible_in_context(context, own_page=None):
    """Conservatively detect an access restriction without inspecting private chats.

    Read ONLY visible dialog/alert text on existing other tabs. The dedicated
    Library page can additionally be scanned by is_challenge(). Never click
    through or dismiss access restrictions automatically.
    """
    for pg in list(context.pages):
        if pg.is_closed() or pg is own_page:
            continue
        try:
            if not pg.url.startswith(('https://chatgpt.com/', 'https://chat.openai.com/')):
                continue
            snippets=pg.evaluate("""() => [...document.querySelectorAll(
                'dialog,[role="dialog"],[role="alertdialog"],[role="alert"]')]
                .filter(e => e.getClientRects().length)
                .map(e => (e.innerText || '').toLowerCase().slice(0, 1200))
                .slice(0, 12)""")
            if any(any(phrase in item for phrase in RESTRICTION_TEXTS)
                   for item in snippets):
                return 'ACCESS_RESTRICTION_IN_EXISTING_TAB'
        except Exception:
            continue
    return None


def confirmed_empty_library_search(page):
    """UI-confirmed empty results; otherwise absence remains inconclusive."""
    try:
        main=page.locator('main')
        if main.count()!=1:
            main=page.locator('[role="main"]')
        if main.count()!=1:
            return False
        content=main.inner_text(timeout=1200).lower()[:18000]
        phrases=(
            'ничего не найдено', 'результатов не найдено',
            'нет результатов', 'не удалось найти файлы',
            'no results found', 'no matching files',
            'no files found', 'nothing found',
        )
        return any(x in content for x in phrases)
    except Exception:
        return False


def _visible_exact_in_main(page, name):
    """Exact filename text nodes in content area, not sidebar/search input."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    candidates=[]
    try:
        for loc in (page.locator('main').get_by_text(name,exact=True),
                    page.locator('[role="main"]').get_by_text(name,exact=True)):
            for i in range(min(loc.count(),20)):
                item=loc.nth(i)
                try:
                    if item.is_visible():
                        candidates.append(item)
                except PlaywrightTimeout:continue
            if candidates:break
    except Exception:pass
    return candidates


def find_file_item(page, name):
    items=_visible_exact_in_main(page,name)
    if not items:return None,'NOT_FOUND'
    # The same filename might appear many times as different Library objects.
    # Do not choose at random; user can map later from a manual download.
    rows=[]
    for e in items:
        try:
            row=e.locator('xpath=ancestor::*[self::tr or @role="row" or @role="listitem" or self::li][1]')
            if row.count() and row.first.is_visible():
                rows.append((e,row.first))
            else:
                rows.append((e,e.locator('xpath=..')))
        except Exception:rows.append((e,e))
    if len(rows)>1:return None,'AMBIGUOUS_VISIBLE_RESULTS'
    return rows[0],None


def button_download(page):
    """Find an unambiguous visible download control in a page or its file preview."""
    label_re = re.compile(r'\bdownload\b|скачать|скачивани', re.I)
    scopes = [page.locator('[role="dialog"]'), page.locator('main'),
              page.locator('[role="main"]'), page.locator('body')]
    for scope in scopes:
        try:
            controls = scope.locator('button, [role="button"], [role="menuitem"], a[download]')
            found = []
            for i in range(min(controls.count(), 150)):
                node = controls.nth(i)
                if not node.is_visible() or not node.is_enabled():
                    continue
                attrs = [(node.get_attribute(a) or '') for a in
                         ('aria-label', 'title', 'data-testid', 'data-state')]
                name = (node.inner_text(timeout=300) or '')[:90]
                if (node.evaluate('(e) => e.tagName === "A" && e.hasAttribute("download")')
                    or label_re.search(' '.join(attrs + [name]))):
                    found.append(node)
            if len(found) == 1:
                return found[0]
        except Exception:
            continue
    return None


def open_file_actions(page, row):
    """Open a clearly labeled file-actions menu only (never delete/edit actions)."""
    patterns = re.compile(r'^(?:more|more options|more actions|actions|ещё|еще|дополнительно|действия(?: с файлом)?|\.\.\.|⋯|…)\s*$', re.I)
    try:
        row.hover(timeout=2000)
        loc = row.locator('button, [role="button"]')
        candidates=[]
        for i in range(min(loc.count(), 16)):
            e=loc.nth(i)
            if not e.is_visible() or not e.is_enabled():continue
            vals=[e.get_attribute(k) or '' for k in ('aria-label','title','data-testid')]
            vals.append((e.inner_text(timeout=400) or '').strip())
            if any(patterns.match(v.strip()) for v in vals):candidates.append(e)
        if len(candidates)==1:
            candidates[0].click(timeout=4000)
            page.wait_for_timeout(500)
            return True
    except Exception:
        pass
    return False


def open_actions_for_filename_row(page, elem):
    """Library list layout: the row's right-hand icon-only ellipsis opens Download.

    Uses the visible exact filename plus the same-line geometry. This is useful
    when a row is built from nested DIVs without <tr>, role=row or role=listitem.
    The candidate must be a small control to the RIGHT of the filename, near
    the same vertical center. Never guess among multiple controls.
    """
    try:
        elem.hover(timeout=2500)
        text_box=elem.bounding_box()
        if not text_box:
            return False
        ty=text_box['y']+text_box['height']/2
        tx=text_box['x']+text_box['width']
        main=page.locator('main')
        if main.count()!=1:
            main=page.locator('[role="main"]')
        if main.count()!=1:
            return False
        controls=main.locator('button, [role="button"]')
        candidates=[]
        for i in range(min(controls.count(),350)):
            b=controls.nth(i)
            if not b.is_visible() or not b.is_enabled():
                continue
            r=b.bounding_box()
            if not r:
                continue
            cy=r['y']+r['height']/2
            # The row ellipsis is a compact icon at far right, not a toolbar
            # command or a checkbox (which sits left of the filename).
            if abs(cy-ty)>20 or r['x']<tx+40 or r['width']>75 or r['height']>65:
                continue
            vals=[(b.get_attribute(key) or '').strip().casefold()
                  for key in ('aria-label','title','data-testid')]
            inner=(b.inner_text(timeout=250) or '').strip().casefold()
            vals.append(inner)
            reject=('delete','remove','удалить','rename','переименовать','share','поделиться')
            if any(any(x in val for x in reject) for val in vals):
                continue
            # A small, icon-only button on the same line is the documented
            # screenshot layout. Prefer explicit action/ellipsis hints.
            good=('more','options','action','menu','ellipsis','действ','ещё','еще','...','⋯','…')
            hinted=any(any(token in val for token in good) for val in vals)
            candidates.append((b,hinted,r['x']))
        if not candidates:
            return False
        hinted=[v for v in candidates if v[1]]
        if len(hinted)==1:
            selected=hinted[0][0]
        elif len(candidates)==1:
            selected=candidates[0][0]
        else:
            return False
        selected.click(timeout=4500)
        page.wait_for_timeout(350)
        # Must verify menu actually contains the exact safe command before
        # reporting a success; no other menu action is ever clicked here.
        return button_download(page) is not None
    except Exception:
        return False


def find_single_download_submenu_option(page):
    """Library preview sometimes opens a second popover instead of downloading.

    Screenshot: top-right down-arrow -> popover caption 'Скачать' -> one
    downloadable document item (its visible text may be an opaque name like '3').
    Only select one *unambiguous* item inside that particular popover.
    Never guess among several candidates, and never select Delete/Share/etc.
    """
    title_re = re.compile(r'^(?:Скачать|Download)$', re.I)
    reject_re = re.compile(r'удалить|delete|remove|поделиться|share|rename|переименовать', re.I)
    try:
        heads = page.get_by_text(title_re, exact=True)
        for h_index in range(min(heads.count(), 12)):
            heading = heads.nth(h_index)
            if not heading.is_visible():
                continue
            hb = heading.bounding_box()
            if not hb:
                continue
            # Need an actual expanded popover with a separate option below the
            # caption, not a direct Download button elsewhere on the page.
            ancestor = heading.locator('xpath=..')
            for _ in range(5):
                if ancestor.count()!=1:
                    break
                rect = ancestor.bounding_box()
                if not rect:
                    break
                if (145 <= rect['width'] <= 520 and 64 <= rect['height'] <= 410
                        and rect['y'] <= hb['y'] and rect['y'] + rect['height'] > hb['y'] + hb['height'] + 18):
                    opts = ancestor.locator('a,button,[role="menuitem"],[role="button"]')
                    clickable=[]
                    for i in range(min(opts.count(), 30)):
                        el=opts.nth(i)
                        if not el.is_visible() or not el.is_enabled():
                            continue
                        r=el.bounding_box()
                        if not r or r['y'] <= hb['y']+hb['height']-4:
                            continue
                        caption=(el.inner_text(timeout=250) or '')+' '+(el.get_attribute('aria-label') or '')
                        if reject_re.search(caption):
                            continue
                        clickable.append(el)
                    if len(clickable)==1:
                        return clickable[0]
                    if len(clickable)>1:
                        return None
                    # Some menus implement the file row as a non-button div.
                    # In that case use the *single visible text leaf* below
                    # the caption. Clicking that leaf bubbles to its row.
                    leafs=ancestor.locator('xpath=.//*[not(*) and normalize-space(string(.)) != ""]')
                    visible=[]
                    for i in range(min(leafs.count(),40)):
                        el=leafs.nth(i)
                        if not el.is_visible():
                            continue
                        r=el.bounding_box()
                        if not r or r['y'] <= hb['y']+hb['height']-3:
                            continue
                        label=(el.inner_text(timeout=200) or '').strip()
                        if not label or title_re.fullmatch(label) or reject_re.search(label):
                            continue
                        visible.append(el)
                    if len(visible)==1:
                        return visible[0]
                    if len(visible)>1:
                        return None
                ancestor=ancestor.locator('xpath=..')
    except Exception:
        pass
    return None


class OwnedPageDownloadObserver:
    """Collect browser-native downloads on the Library tab AND its popups.

    Important: does not inspect files, cookies, response bodies, or unrelated
    pre-existing tabs in the signed-in Chrome profile. Only watches the
    temporary Library tab plus popup tabs opened by it.
    """
    def __init__(self, page):
        self.page = page
        self.context = page.context
        self.owned = set()
        self.events = []
        self._hooked = []
        self.context.on('page', self._on_page)
        self._hook(page)

    def _hook(self, pg):
        if pg in self.owned:
            return
        self.owned.add(pg)
        pg.on('download', self._on_download)
        pg.on('popup', self._on_popup)
        self._hooked.append(pg)

    def _on_popup(self, pg):
        self._hook(pg)

    def _on_page(self, pg):
        try:
            parent = pg.opener()
            if parent in self.owned:
                self._hook(pg)
        except Exception:
            pass

    def _on_download(self, download):
        self.events.append(download)

    def wait_download(self, timeout_ms):
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        while time.monotonic() < deadline and not self.events:
            active = next((pg for pg in self.owned if not pg.is_closed()), None)
            if active is None:
                return None
            active.wait_for_timeout(min(250, max(1, int((deadline-time.monotonic())*1000))))
        return self.events[0] if self.events else None

    def close(self):
        try: self.context.remove_listener('page', self._on_page)
        except Exception: pass
        for pg in self._hooked:
            try: pg.remove_listener('download', self._on_download)
            except Exception: pass
            try: pg.remove_listener('popup', self._on_popup)
            except Exception: pass


def save_browser_download(page, btn, tmp_dir, wait_ms):
    """Download through *visible* controls, tracking events in preview popups.

    A click may either trigger a native download or show a one-item file
    selection popover. Never click a second time on a download option: one
    click per stage, then wait. Unknown layouts stop and produce diagnostics.
    """
    obs = OwnedPageDownloadObserver(page)
    downloaded = None
    try:
        btn.click(timeout=8000)
        downloaded = obs.wait_download(min(wait_ms, 3500))
        if downloaded is None:
            # The first click may have opened 'Скачать' -> <single file>.
            option = find_single_download_submenu_option(page)
            if option is None:
                # A direct native download can arrive later than the initial
                # observation window. Do not click again or guess a control.
                downloaded = obs.wait_download(max(0, wait_ms - 3500))
                if downloaded is None:
                    return None, 'DOWNLOAD_SUBMENU_UNRECOGNIZED'
            else:
                option.click(timeout=8000)
                downloaded = obs.wait_download(wait_ms)
                if downloaded is None:
                    return None, 'NO_BROWSER_DOWNLOAD_EVENT_AFTER_MENU'
        # Avoid silently choosing one of several simultaneous downloads.
        if len(obs.events) != 1:
            return None, 'AMBIGUOUS_BROWSER_DOWNLOAD_EVENTS'
        if downloaded.failure():
            return None, 'BROWSER_DOWNLOAD_FAILED'
        tmp_dir.mkdir(parents=True,exist_ok=True)
        temp=tmp_dir/f'{int(time.time()*1000)}_{safe_part(downloaded.suggested_filename)}'
        downloaded.save_as(str(temp))
        return temp, None
    finally:
        obs.close()



def attempt_named_row_download_after_preview(page, filename, tmp_dir, wait_ms=20000,
                                             allow_action=None):
    """One fallback through an explicitly named Library file-actions button.

    Only used when the *first* visible-preview route returned no browser download
    event. The diagnostic shows both the open file viewer and an accessible
    button 'Открыть меню действий для «<filename>»' in its original row.

    Avoid guessing among action buttons or repeating the same preview click.
    In particular, do nothing if a real 429/403/account warning has appeared.
    No network requests are constructed manually; only normal Library controls.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    def ok():
        try:
            return (allow_action is None or bool(allow_action())) and not is_challenge(page)
        except Exception:
            return False

    if not ok():
        return None, 'ROW_FALLBACK_ACCESS_STOP'

    # This is the exact viewer Close control returned in the user's UI probe.
    # If it is absent, the route is unknown. Do not close anything else.
    try:
        close = page.get_by_role('button', name=re.compile(
            r'^(?:Закрыть средство просмотра|Close (?:viewer|preview))$', re.I))
        close_visible = close.filter(visible=True)
        if close_visible.count() != 1:
            return None, 'ROW_FALLBACK_VIEWER_NOT_IDENTIFIED'
        close_visible.first.click(timeout=4500)
        page.wait_for_timeout(400)
    except PlaywrightTimeout:
        return None, 'ROW_FALLBACK_VIEWER_CLOSE_TIMEOUT'
    except Exception:
        return None, 'ROW_FALLBACK_VIEWER_CLOSE_FAILED'

    if not ok():
        return None, 'ROW_FALLBACK_ACCESS_STOP'

    # Match the *exact file* and not any other row's action menu. This name
    # was observed in the user's current Library diagnostics.
    action_name = 'Открыть меню действий для «' + filename + '»'
    try:
        actions = page.get_by_role('button', name=action_name, exact=True).filter(visible=True)
        if actions.count() != 1:
            return None, 'ROW_FALLBACK_NAMED_ACTION_MISSING'
        actions.first.click(timeout=5500)
        page.wait_for_timeout(350)
    except PlaywrightTimeout:
        return None, 'ROW_FALLBACK_ACTION_TIMEOUT'
    except Exception:
        return None, 'ROW_FALLBACK_ACTION_FAILED'

    if not ok():
        return None, 'ROW_FALLBACK_ACCESS_STOP'

    # Prefer the explicit one-item Download action inside the just-opened
    # menu. If UI uses a button rather than menuitem, button_download will
    # accept it only when unambiguous. Never guess an unlabeled icon.
    try:
        menu_item = page.get_by_role('menuitem', name=re.compile(
            r'^(?:Скачать|Download)$', re.I)).filter(visible=True)
        if menu_item.count() == 1:
            btn = menu_item.first
        elif menu_item.count() > 1:
            return None, 'ROW_FALLBACK_MULTIPLE_DOWNLOAD_ACTIONS'
        else:
            btn = button_download(page)
        if btn is None:
            return None, 'ROW_FALLBACK_DOWNLOAD_NOT_VISIBLE'
        return save_browser_download(page, btn, tmp_dir, wait_ms)
    except PlaywrightTimeout:
        return None, 'ROW_FALLBACK_DOWNLOAD_TIMEOUT'
    except Exception:
        return None, 'ROW_FALLBACK_DOWNLOAD_FAILED'


def ui_probe_v2(page, context, out_dir, filename, stage):
    """Diagnostic of action controls only: no page HTML, cookies, tokens or full filenames."""
    def inspect(tab):
        try:
            return tab.evaluate("""() => {
                const main = document.querySelector('main,[role="main"]');
                const roots = [main, ...document.querySelectorAll('[role="dialog"],[role="menu"]')].filter(Boolean);
                const safe = (e) => ({
                  role: (e.getAttribute('role') || e.tagName.toLowerCase()).slice(0,24),
                  label: (e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText || '').trim().slice(0,90),
                  testid: (e.getAttribute('data-testid') || '').slice(0,70)
                });
                const controls = roots.flatMap(r => [...r.querySelectorAll('button,[role="button"],[role="menuitem"],a[download]')]
                   .filter(e=>e.getClientRects().length).slice(0,45).map(safe));
                return {url:location.origin+location.pathname, main_present:!!main,
                  main_visible:!!(main && main.getClientRects().length),
                  dialogs:document.querySelectorAll('[role="dialog"]').length,
                  menus:document.querySelectorAll('[role="menu"]').length,
                  visible_controls:controls.slice(0,45)};
            }""")
        except Exception as ex:return {'error':type(ex).__name__}
    # Only inspect our temporary Library tab and popups opened by it. Do not
    # scan unrelated signed-in ChatGPT tabs in the user's Chrome profile.
    related = [page]
    for pg in list(context.pages):
        if pg is page or pg.is_closed():
            continue
        try:
            if pg.opener() in related:
                related.append(pg)
        except Exception:
            pass
    entries = [inspect(pg) for pg in related if not pg.is_closed()]
    d={'stage':stage,'filename_expected':filename,'pages':entries,
       'diagnostic_note':'Temporary Library tab and its popups only; no tokens, cookies, HTML or request URLs'}
    out_dir.mkdir(parents=True, exist_ok=True)
    path=out_dir/'library_ui_probe_v2.json'
    path.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf8')
    print('Расширенная диагностика интерфейса:',path)


def download_one_ui(page, search_box, filename, tmp_dir, wait_ms=20000):
    """Use Library row ellipsis first, then fallback to selection/preview."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    stage='START'
    try:
        stage='SEARCH'
        search_box.fill(filename,timeout=7000)
        page.wait_for_timeout(1300)
        if is_challenge(page):
            return None,'SECURITY_CHECK'
        pair,error=find_file_item(page,filename)
        if error=='NOT_FOUND':
            if is_challenge(page):
                return None,'SECURITY_CHECK'
            # The Library search can finish after the keypress; a 1.3s check
            # may incorrectly label a slow search as a missing file.
            search_box.press('Enter')
            deadline=time.monotonic()+8.0
            while time.monotonic()<deadline:
                if is_challenge(page):
                    return None,'SECURITY_CHECK'
                pair,error=find_file_item(page,filename)
                if error!='NOT_FOUND':
                    break
                page.wait_for_timeout(400)
        if error=='NOT_FOUND':
            return None, ('NOT_FOUND_IN_LIBRARY_SEARCH'
                          if confirmed_empty_library_search(page)
                          else 'NOT_FOUND_UNCONFIRMED')
        if error:return None,error
        elem,row=pair
        context=page.context
        pages_before=set(context.pages)
        stage='ROW_FOUND'
        # The screenshot shows Library actions under the far-right "..."
        # on the same line as the file. Try that first, BEFORE opening a
        # preview or changing row selection.
        try:
            already=button_download(page)
            if already:
                stage='EXISTING_DOWNLOAD_MENU'
                return save_browser_download(page,already,tmp_dir,wait_ms)
            stage='ROW_ELLIPSIS'
            if open_actions_for_filename_row(page,elem):
                found=button_download(page)
                if found:
                    stage='ROW_MENU_DOWNLOAD'
                    return save_browser_download(page,found,tmp_dir,wait_ms)
        except PlaywrightTimeout:
            return None,'NO_BROWSER_DOWNLOAD_EVENT'
        except Exception:
            pass
        stage='ROW_FOUND'
        # The download toolbar may be hidden until the file is selected.
        try:row.hover(timeout=2500)
        except Exception:pass
        checkbox=row.locator('input[type="checkbox"], [role="checkbox"]')
        did_select=False
        if checkbox.count()==1 and checkbox.first.is_visible():
            checkbox.first.click(timeout=4500)
            did_select=True
        else:
            stage='OPEN_FILE_PREVIEW'
            elem.click(timeout=7000)
        # It takes time to render a preview or selected-file toolbar.
        stage='WAIT_DOWNLOAD_CONTROL'
        for _ in range(12):
            if is_challenge(page):return None,'SECURITY_CHECK'
            candidates=[page]+[p for p in context.pages if p not in pages_before and p is not page and not p.is_closed()]
            for active in candidates[::-1]:
                try:
                    btn=button_download(active)
                    if btn:
                        stage='CLICK_DOWNLOAD'
                        return save_browser_download(active,btn,tmp_dir,wait_ms)
                except PlaywrightTimeout:return None,'NO_BROWSER_DOWNLOAD_EVENT'
                except Exception:continue
            page.wait_for_timeout(350)
        # A file menu sometimes exposes Download only after the actions menu opens.
        if open_file_actions(page,row):
            for _ in range(5):
                btn=button_download(page)
                if btn:
                    stage='MENU_DOWNLOAD'
                    try:
                        return save_browser_download(page,btn,tmp_dir,wait_ms)
                    except PlaywrightTimeout:return None,'NO_BROWSER_DOWNLOAD_EVENT'
                page.wait_for_timeout(400)
        # Save richer state on mismatch. Note that current UI may use a popup.
        ui_probe_v2(page,context,tmp_dir.parent,filename,
                    'NO_DOWNLOAD_CONTROL_AFTER_'+('CHECKBOX' if did_select else 'PREVIEW'))
        return None,'DOWNLOAD_BUTTON_NOT_FOUND'
    except Exception as exc:
        try:ui_probe_v2(page,page.context,tmp_dir.parent,filename,stage+'_'+type(exc).__name__)
        except Exception:pass
        return None,'UI_ERROR_'+type(exc).__name__


def same_file_bytes(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):h.update(chunk)
    return h.hexdigest()


def downloaded_content_type(src):
    """Recognize common error pages without logging file contents."""
    with src.open('rb') as fh:
        header=fh.read(4096).lstrip(b'\xef\xbb\xbf\x00\x20\r\n\t').lower()
    if header.startswith((b'<!doctype html', b'<html')):
        return 'HTML_ERROR_PAGE'
    if header.startswith(b'{') and (b'"not found"' in header or b'"unauthorized"' in header):
        if b'"detail"' in header or b'"error"' in header:
            return 'JSON_ERROR_RESPONSE'
    if header.startswith(b'%pdf-'):
        return 'PDF'
    if header.startswith(b'pk\x03\x04'):
        return 'ZIP'
    if header.startswith((b'\x89png\r\n\x1a\n', b'gif87a', b'gif89a', b'\xff\xd8\xff')):
        return 'IMAGE'
    try:
        header.decode('utf-8-sig')
        return 'UTF8_TEXT_OR_OTHER'
    except UnicodeError:
        return 'BINARY_OR_NON_UTF8'


def quarantine_mismatched_download(src, target, backup):
    """Keep nonmatching bytes *outside* gpts/<...>/knowledge with metadata.

    This is an unverified Library download, NOT an original Knowledge backup.
    It will never be copied into a GPT by this function.
    """
    digest=same_file_bytes(src)
    actual=src.stat().st_size
    root=(backup/'Library_mismatch_quarantine'/
          safe_part(target['gpt_id'],90)/safe_part(target['file_id'],90))
    root.mkdir(parents=True,exist_ok=True)
    quarantine=root/(digest+'.bin')
    if quarantine.exists():
        if same_file_bytes(quarantine)!=digest or quarantine.stat().st_size!=actual:
            raise RuntimeError('QUARANTINE_HASH_CONFLICT')
        src.unlink(missing_ok=True)
    else:
        # Source is a transient native-browser download in the same backup.
        # Rename preserves bytes and leaves the original Knowledge folder alone.
        src.replace(quarantine)
    meta={
        'warning':'UNVERIFIED_LIBRARY_DOWNLOAD_NOT_ORIGINAL_KNOWLEDGE',
        'gpt_name':target['gpt_name'],
        'gpt_id':target['gpt_id'],
        'file_id':target['file_id'],
        'expected_name':target['name'],
        'expected_bytes':target['size'],
        'received_bytes':actual,
        'received_sha256':digest,
        'content_type_hint':downloaded_content_type(quarantine),
        'saved_as':str(quarantine),
        'recorded_utc':datetime.now(timezone.utc).isoformat(timespec='seconds')
    }
    metadata=quarantine.with_suffix('.json')
    tmp=metadata.with_suffix('.json.partial')
    tmp.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    tmp.replace(metadata)
    return meta


def candidate_folder(backup, target):
    return (backup/'Library_phase2_candidates'/safe_part(target['gpt_id'],90)
            /safe_part(target['file_id'],90))


def candidate_present(backup, target, digest):
    if not re.fullmatch(r'[a-fA-F0-9]{64}', digest or ''):
        return False
    p=candidate_folder(backup,target)/(digest.lower()+'.bin')
    return p.is_file() and p.stat().st_size==target['size'] and same_file_bytes(p)==digest.lower()


def stage_candidate(src, target, backup):
    """Never write into gpts/.../knowledge for a Library-sourced file.

    Even identical filename and size do NOT prove original GPT file identity.
    """
    if src.stat().st_size != target['size']:
        return 'WRONG_SIZE', '', ''
    digest=same_file_bytes(src)
    folder=candidate_folder(backup,target)
    dest=folder/(digest+'.bin')
    meta=folder/(digest+'.json')
    folder.mkdir(parents=True,exist_ok=True)
    created=False
    if dest.exists():
        if not candidate_present(backup,target,digest):
            raise RuntimeError('LIBRARY_CANDIDATE_HASH_CONFLICT')
    else:
        # No overwrite, even on interrupted previous runs.
        try:
            with dest.open('xb') as output, src.open('rb') as source:
                created=True
                shutil.copyfileobj(source,output,1024*1024)
            if not candidate_present(backup,target,digest):
                raise RuntimeError('LIBRARY_CANDIDATE_VERIFY_FAILED')
        except BaseException:
            if created:dest.unlink(missing_ok=True)
            raise
    evidence={
        'schema':1,
        'status':'LIBRARY_CANDIDATE_NOT_DIRECT_GPT_ORIGINAL',
        'warning':'Matching filename and size do NOT independently prove identical GPT original bytes.',
        'gpt_name':target['gpt_name'], 'gpt_id':target['gpt_id'],
        'file_name':target['name'], 'file_id':target['file_id'],
        'snapshot_expected_bytes':target['size'], 'downloaded_bytes':src.stat().st_size,
        'sha256':digest,'candidate_path':str(dest),
        'source':'visible_ChatGPT_Library_UI',
        'original_knowledge_destination_untouched':str(target['dest']),
        'created_utc':datetime.now(timezone.utc).isoformat(timespec='seconds')
    }
    if not meta.exists():
        with meta.open('x',encoding='utf-8') as f:
            json.dump(evidence,f,ensure_ascii=False,indent=2)
    return ('EXISTING_CANDIDATE' if not created else 'STAGED'), str(dest), digest


def safe_copy(src,dst,expected):
    if dst.exists():
        return 'EXISTING' if maybe_valid(dst,expected) else 'EXISTING_SIZE_MISMATCH'
    if src.stat().st_size!=expected:return 'WRONG_SIZE'
    dst.parent.mkdir(parents=True,exist_ok=True)
    partial=dst.with_suffix(dst.suffix+'.part-library')
    try:
        shutil.copyfile(src,partial)
        if partial.stat().st_size!=expected:return 'WRONG_SIZE'
        partial.replace(dst)
        return 'COPIED'
    finally:
        if partial.exists():partial.unlink()


def report(path, records):
    """Save queue status atomically after every result (no private credentials)."""
    path.parent.mkdir(parents=True,exist_ok=True)
    fields=['gpt_name','gpt_id','file_id','file_name','expected_bytes','status','saved_path','sha256','note']
    tmp=path.with_suffix(path.suffix+'.partial')
    with tmp.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,delimiter=';',extrasaction='ignore')
        w.writeheader()
        for t in records:
            w.writerow({'gpt_name':t.get('gpt_name'),'gpt_id':t.get('gpt_id'),
                        'file_id':t.get('file_id'),'file_name':t.get('name'),
                        'expected_bytes':t.get('size'),'status':t.get('status','PENDING'),
                        'saved_path':(str(t.get('candidate_path','')) if t.get('status')=='LIBRARY_CANDIDATE_STAGED' else (str(t.get('dest')) if t.get('status')=='ALREADY_EXISTS' else '')),
                        'sha256':t.get('sha256',''),
                        'note':t.get('note','')})
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp,path)


def build_jobs(targets):
    grouped=collections.defaultdict(list)
    for t in targets:grouped[norm(t['name'])].append(t)
    # Give first priority to names which are unique across all target GPTs.
    return sorted(grouped.items(),key=lambda p:(len(p[1])>1,len(p[1]),p[0]))


def write_offline_audit(backup, targets, all_targets, previously_not_found):
    """Read local files and snapshot only. NEVER touches browser or network."""
    group=collections.defaultdict(list)
    for t in all_targets:
        group[norm(t['name'])].append(t)
    fields=['gpt_name','gpt_id','file_id','file_name','expected_bytes',
            'local_bytes','local_status','library_lookup','duplicate_name_ids','local_path']
    detail=backup/'library_offline_audit.csv'
    summary_path=backup/'library_offline_gpt_summary.csv'
    detail.parent.mkdir(parents=True,exist_ok=True)
    counters=collections.Counter()
    by_gpt=collections.defaultdict(lambda: {'name':'','expected':0,'present':0,
                                             'absent':0,'size_mismatch':0,'ambiguous':0})
    with detail.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,delimiter=';')
        w.writeheader()
        for t in targets:
            dest=t['dest']
            actual=dest.stat().st_size if dest.is_file() else 0
            if actual==t['size']:
                status='PRESENT_SIZE_MATCH'
            elif actual>0:
                status='LOCAL_SIZE_MISMATCH'
            else:
                status='ABSENT_LOCALLY'
            record=(t['gpt_id'],t['file_id'],t['size'])
            missed='PREVIOUS_SEARCH_MISS_UNCONFIRMED' if record in previously_not_found else 'NOT_TESTED_OR_NOT_RECORDED'
            ids={x['file_id'] for x in group[norm(t['name'])]}
            ambiguous=len(ids)>1
            w.writerow({'gpt_name':t['gpt_name'],'gpt_id':t['gpt_id'],
                        'file_id':t['file_id'],'file_name':t['name'],
                        'expected_bytes':t['size'],'local_bytes':actual,
                        'local_status':status,'library_lookup':missed,
                        'duplicate_name_ids':len(ids),'local_path':str(dest)})
            counters[status]+=1
            if ambiguous:counters['AMBIGUOUS_NAME_IDS']+=1
            if record in previously_not_found:counters['PRIOR_SEARCH_MISSES']+=1
            rec=by_gpt[t['gpt_id']]
            rec['name']=t['gpt_name']
            rec['expected']+=1
            if status=='PRESENT_SIZE_MATCH':rec['present']+=1
            elif status=='LOCAL_SIZE_MISMATCH':rec['size_mismatch']+=1
            else:rec['absent']+=1
            if ambiguous:rec['ambiguous']+=1
    with summary_path.open('w',encoding='utf-8-sig',newline='') as f:
        keys=['gpt_name','gpt_id','expected','present','absent','size_mismatch','ambiguous_names']
        w=csv.DictWriter(f,fieldnames=keys,delimiter=';');w.writeheader()
        for gid,entry in sorted(by_gpt.items(),key=lambda z:(-z[1]['absent'],z[1]['name'])):
            w.writerow({'gpt_name':entry['name'],'gpt_id':gid, 'expected':entry['expected'],
                        'present':entry['present'],'absent':entry['absent'],
                        'size_mismatch':entry['size_mismatch'],'ambiguous_names':entry['ambiguous']})
    print('ЛОКАЛЬНЫЙ АУДИТ. Без обращения к ChatGPT или Chrome.')
    print('Knowledge-записей:',len(targets),'| GPT с Knowledge:',len(by_gpt))
    print('Есть на диске с ожидаемым размером:',counters['PRESENT_SIZE_MATCH'])
    print('Не найдено локально:',counters['ABSENT_LOCALLY'])
    print('Есть, но размер отличается:',counters['LOCAL_SIZE_MISMATCH'])
    print('Записей с неоднозначным именем/file_id:',counters['AMBIGUOUS_NAME_IDS'])
    print('Ранее не обнаружены поиском Library (не доказательство отсутствия):',counters['PRIOR_SEARCH_MISSES'])
    print('Подробный отчёт:',detail)
    print('Сводка по GPT:',summary_path)
    return 0



class OwnedLibraryAccessMonitor:
    """Observe ONLY the temporary Library page and its own popup pages.

    page.on('response') delivers responses owned by that page, unlike
    context.on('response'), which also receives unrelated chats' API traffic.
    No content from other tabs, response bodies, or private URLs is read.
    """
    def __init__(self, page, state):
        self.state=state
        self.owned=set()
        self.listeners=[]
        self.context=page.context
        self.context.on('page',self._on_context_page)
        self.attach(page,'library_page')

    def attach(self, page, source):
        if page in self.owned:
            return
        self.owned.add(page)
        callback=lambda response, origin=source: self._on_response(response, origin)
        popup_callback=lambda child:self.attach(child,'library_popup')
        page.on('response',callback)
        page.on('popup',popup_callback)
        self.listeners.extend([(page,'response',callback),(page,'popup',popup_callback)])

    def _on_context_page(self, page):
        try:
            if page.opener() in self.owned:
                self.attach(page,'library_popup')
        except Exception:
            pass

    def _on_response(self, response, origin):
        try:
            from urllib.parse import urlsplit
            u=urlsplit(response.url)
            if not (u.hostname=='chatgpt.com'
                    and response.status in ACCESS_DENIAL
                    and any(x in u.path for x in ('/library','/files','/backend-api'))):
                return
            self.state['status']=response.status
            self.state['source']=origin
            self.state['path_class']=('library' if '/library' in u.path else
                                      'files' if '/files' in u.path else 'backend_api')
            if response.status==429:
                try:
                    ra=retry_after_seconds(response.headers.get('retry-after'))
                    if ra is not None:
                        old=self.state['retry_after']
                        self.state['retry_after']=max(float(old or 0),ra)
                except Exception:
                    pass
        except Exception:
            pass

    def visible_warning(self):
        return any(is_challenge(pg) for pg in list(self.owned)
                   if not pg.is_closed())

    def close(self):
        try:self.context.remove_listener('page',self._on_context_page)
        except Exception:pass
        for page,event,handler in self.listeners:
            try:page.remove_listener(event,handler)
            except Exception:pass


def main(argv=None):
    ap=argparse.ArgumentParser(description='V15: Library только кандидатные копии, карантин неверного размера; меню строки; обычная пауза 15–30 сек.; HTTP 429 с Retry-After')
    ap.add_argument('--snapshot', default='Custom_GPT_backup/gpt_list_snapshot.json')
    ap.add_argument('--backup', default='Custom_GPT_backup')
    ap.add_argument('--queue', default='Custom_GPT_backup/Library_phase2_plan/library_queue_missing_unique.csv',
                    help='CSV очереди только оставшихся однозначных имён, подготовленной офлайн-скриптом')
    ap.add_argument('--profile', default='.chatgpt_backup_normal_chrome_profile')
    ap.add_argument('--attach', action='store_true')
    ap.add_argument('--cdp-port', type=int)
    ap.add_argument('--plan-only', action='store_true',
                    help='Без Chrome и сети: сверить очередь с snapshot и существующими файлами')
    ap.add_argument('--confirm-access-restored', action='store_true',
                    help='Я вручную убедился, что ограничение доступа больше не показывается')
    ap.add_argument('--retry-recorded', action='store_true',
                    help='По решению пользователя повторить ранее завершённые неудачные поиски')
    ap.add_argument('--retry-ui-mismatch', action='store_true',
                    help='Повторить только файлы с прежними ошибками интерфейса, не трогая NOT_FOUND')
    ap.add_argument('--retry-wrong-size', action='store_true',
                    help='После изучения размера разрешить повтор ранее отмеченных WRONG_SIZE/ERROR_RESPONSE_NOT_FILE')
    ap.add_argument('--max-searches', type=int, default=1)
    ap.add_argument('--max-files', type=int, default=1)
    ap.add_argument('--ui-wait', type=float, default=30)
    ap.add_argument('--pause', type=float, default=None,
                    help='Необязательная ФИКСИРОВАННАЯ пауза между поисками; перекрывает --pause-min/--pause-max')
    ap.add_argument('--pause-min', type=float, default=15,
                    help='Минимальная обычная пауза между поисками Library (по умолчанию 15 сек.)')
    ap.add_argument('--pause-max', type=float, default=30,
                    help='Максимальная обычная пауза между поисками Library (по умолчанию 30 сек.)')
    ap.add_argument('--retry-429-after', type=float, default=300,
                    help='Минимальная пауза после обычного HTTP 429 (по умолчанию 300 сек); Retry-After имеет приоритет, если больше')
    ap.add_argument('--max-429-retries', type=int, default=0,
                    help='Автоматические повторы после HTTP 429: 0 (по умолчанию, стоп) или 1 по явному выбору')
    args=ap.parse_args(argv)
    if args.max_searches<1 or args.max_files<1 or args.max_searches>250 or args.max_files>250:
        ap.error('В v15 разрешено 1–250 поисков/кандидатов за запуск; безлимитный режим отключён')
    if args.pause is not None:
        # Compatibility with previous v8–v11 --pause commands.
        # Do not use --pause when a varying 15–30 second interval is wanted.
        args.pause_min = args.pause_max = args.pause
    if (not math.isfinite(args.pause_min) or not math.isfinite(args.pause_max)
            or args.pause_min < 0 or args.pause_max < args.pause_min
            or not math.isfinite(args.ui_wait) or args.ui_wait <= 0):
        ap.error('Неверный диапазон обычной паузы/таймаут (0 ≤ --pause-min ≤ --pause-max)')
    if not 0 <= args.max_429_retries <= 1:
        ap.error('Допускается максимум один автоматический повтор после HTTP 429')
    if not 0 <= args.retry_429_after <= MAX_AUTOMATIC_COOLDOWN:
        ap.error('Неверная пауза после 429 (0–3600 секунд)')
    if not args.plan_only:
        if not args.attach:ap.error('Для сетевого режима нужен --attach; для проверки --plan-only')
        if not args.confirm_access_restored:
            ap.error('Сетевой режим заблокирован: сначала убедись, что предупреждение '
                     '«Слишком много запросов» исчезло, затем укажи --confirm-access-restored')
    src=Path(args.snapshot).expanduser().resolve()
    backup=Path(args.backup).expanduser().resolve()
    queue_file=Path(args.queue).expanduser().resolve()
    if not src.is_file():ap.error(f'Не найден snapshot: {src}')
    all_targets=parse_snapshot(src,backup)
    try:
        selected=load_validated_queue(queue_file,all_targets)
    except (OSError,ValueError,csv.Error) as exc:
        ap.error(str(exc))
    report_file=backup/'library_phase2_candidate_inventory.csv'
    previous=read_queue_progress(report_file)
    totals=init_queue_status(selected,previous,args.retry_recorded,args.retry_ui_mismatch,
                             args.retry_wrong_size)
    if args.plan_only:return offline_queue_preview(backup, selected)
    pending=totals['PENDING']
    print(f'В очереди {len(selected)} оригинальных Knowledge-записей, '
          f'уже на диске {totals["ALREADY_EXISTS"]}, ожидают поиска {pending}.')
    print(f'Предыдущих результатов без повтора: {len(selected)-pending-totals["ALREADY_EXISTS"]}.')
    print('Источник: видимая Library. Всё полученное сохраняется как КАНДИДАТЫ отдельно от originals.')
    print(f'Обычная пауза между поисками: {args.pause_min:g}–{args.pause_max:g} сек.; '
          f'после HTTP 429 — не менее {args.retry_429_after:.0f} сек. и не ранее Retry-After; '
          f'автоматических повторов одного файла: {args.max_429_retries}.')
    # Separate phase2 inventory. Do not alter legacy v7/v14 progress or direct-GPT receipts.
    report(report_file,selected)
    if not pending:return 0
    jobs=[(norm(t['name']), [t]) for t in selected]  # Keep CSV queue order.
    global_names=collections.defaultdict(set)
    for t in all_targets:global_names[norm(t['name'])].add(t['file_id'])
    from playwright.sync_api import sync_playwright
    p=None;tab=None;context=None;access_monitor=None
    new_count=0;searches=0;stop=None
    limit_state={'status':None,'retry_after':None,'source':None,'path_class':None}
    try:
        port=existing_chrome_port(Path(args.profile).expanduser().resolve(),args.cdp_port)
        p=sync_playwright().start()
        browser=p.chromium.connect_over_cdp(f'http://127.0.0.1:{port}',timeout=20000)
        if not browser.contexts:raise RuntimeError('Не найден контекст Chrome')
        context=browser.contexts[0]
        if restriction_visible_in_context(context):
            print('Остановка: интерфейс ChatGPT показывает ограничение доступа.');return 3
        tab=context.new_page()
        # Watch responses from the temporary Library tab (and its popups) ONLY.
        # In v10 a context-wide listener falsely attributed another chat's
        # 429 to the Library run and stopped before the first search.
        access_monitor=OwnedLibraryAccessMonitor(tab, limit_state)

        def startup_stop(reason):
            # The next file stays PENDING. Never overwrite prior successful rows.
            log_rate_limit(backup,'<before_first_search>',reason,
                           server_retry_after=limit_state['retry_after'])
            print('Остановка:',reason)
            print('Диагностика: источник HTTP-ответа:',limit_state['source'] or 'не определён',
                  '; раздел:',limit_state['path_class'] or 'не определён')
            print(f'Результат: поисков {searches}; новых файлов {new_count}.')
            print('Журнал:',report_file)
            return 3

        tab.goto(BASE,wait_until='domcontentloaded',timeout=60000)
        search,reason=wait_library_search(tab,args.ui_wait)
        if limit_state['status']==429:
            print('HTTP 429 при первоначальном открытии Library (до первого поиска).')
            print('Источник ответа:',limit_state['source'] or 'unknown',
                  '; раздел:',limit_state['path_class'] or 'unknown')
            if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                return startup_stop('SECURITY_RESTRICTION')
            if limit_state['source'] not in ('library_page','library_popup'):
                return startup_stop('HTTP_429_FROM_OTHER_TAB_OR_UNKNOWN_ORIGIN')
            if args.max_429_retries==0:
                return startup_stop('HTTP_429_STARTUP_RETRIES_DISABLED')
            wait_sec=cooldown_seconds(args.retry_429_after,limit_state['retry_after'])
            if wait_sec>MAX_AUTOMATIC_COOLDOWN:
                return startup_stop('SERVER_RETRY_AFTER_TOO_LONG')
            print(f'Пауза {wait_sec:.0f} сек. по HTTP 429 до одного повторного открытия Library.',flush=True)
            log_rate_limit(backup,'<before_first_search>','STARTUP_WAIT_THEN_RETRY_ONCE',
                           wait_sec,limit_state['retry_after'])
            time.sleep(wait_sec)
            if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                return startup_stop('SECURITY_RESTRICTION')
            limit_state.update(status=None,retry_after=None,source=None,path_class=None)
            try:
                tab.goto(BASE,wait_until='domcontentloaded',timeout=60000)
                search,reason=wait_library_search(tab,args.ui_wait)
            except Exception as exc:
                print('Не удалось открыть Library после ожидания:',type(exc).__name__)
                return startup_stop('LIBRARY_REOPEN_FAILED')
            if limit_state['status'] is not None:
                return startup_stop('HTTP_ACCESS_RESTRICTION_'+str(limit_state['status']))
        elif limit_state['status'] is not None:
            return startup_stop('HTTP_ACCESS_RESTRICTION_'+str(limit_state['status']))
        if search is None:
            stop=reason
            ui_probe(tab,backup,reason)
            print('Остановка:',reason)
            print(f'Результат: поисков {searches}; новых файлов {new_count}.')
            return 3
        consecutive_ui_failures=0
        for _, matches in jobs:
            needed=[t for t in matches if t['status']=='PENDING']
            if not needed:continue
            if searches>=args.max_searches:stop='SEARCH_LIMIT';break
            if new_count>=args.max_files:stop='DOWNLOAD_LIMIT';break
            # Strict queue guarantees unique file IDs by normalized name.
            if len(global_names[norm(needed[0]['name'])])>1:
                for t in needed:
                    t['status']='AMBIGUOUS_SAME_NAME'
                    t['note']='Невозможно однозначно связать с исходным file_id'
                report(report_file,selected)
                continue
            if limit_state['status'] is not None:
                stop='HTTP_ACCESS_RESTRICTION_'+str(limit_state['status']);break
            if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                stop='SECURITY_RESTRICTION';break
            if searches:
                tab.goto(BASE,wait_until='domcontentloaded',timeout=60000)
                search,reason=wait_library_search(tab,args.ui_wait)
                if search is None:
                    stop=reason
                    ui_probe(tab,backup,reason)
                    break
                # The navigation itself can be rate-limited; do not launch
                # another search on a page that has already returned 429.
                if limit_state['status'] is not None:
                    stop='HTTP_ACCESS_RESTRICTION_'+str(limit_state['status'])
                    break
                if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                    stop='SECURITY_RESTRICTION'
                    break
                # This pause distributes normal UI work; it never overrides HTTP 429.
                normal_delay = random.uniform(args.pause_min, args.pause_max)
                print(f'  Пауза до следующего поиска: {normal_delay:.1f} сек.', flush=True)
                time.sleep(normal_delay)
            searches+=1
            name=needed[0]['name']
            print(f'[{searches}/{args.max_searches}] Library: {name}')
            retries_429=0
            file=None
            error=None
            while True:
                # Any protective login/captcha/account restriction is a hard stop,
                # even if a server also emitted HTTP 429. We never dismiss it.
                if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                    stop='SECURITY_RESTRICTION'
                    break
                file,error=download_one_ui(tab,search,name,backup/'Library_downloads_temp')
                # The probe for 05_PROBLEM_DEFINITION_METHOD.md showed an
                # open viewer, a visible Download toolbar button, and the
                # file's named row-actions menu, but NO download event after
                # the preview's submenu was clicked. Try exactly one distinct
                # UI path before treating this as a terminal interface issue.
                # Never do this after a server restriction or account warning.
                if (file is None and error in (
                        'NO_BROWSER_DOWNLOAD_EVENT_AFTER_MENU',
                        'DOWNLOAD_SUBMENU_UNRECOGNIZED')
                        and limit_state['status'] is None
                        and not restriction_visible_in_context(context, own_page=tab)
                        and not access_monitor.visible_warning()):
                    tab.wait_for_timeout(400)  # Delayed access-denial response may surface.
                    if (limit_state['status'] is None
                            and not restriction_visible_in_context(context, own_page=tab)
                            and not access_monitor.visible_warning()):
                        print('  → Просмотрщик не дал загрузку; проверяю меню «⋯» '
                              'этого файла (одна альтернативная попытка).', flush=True)
                        second, second_err = attempt_named_row_download_after_preview(
                            tab, name, backup/'Library_downloads_temp',
                            allow_action=lambda: (
                                limit_state['status'] is None
                                and not restriction_visible_in_context(context, own_page=tab)
                                and not access_monitor.visible_warning()))
                        if second is not None:
                            file,error=second,None
                        else:
                            # No browser download: record exact stage for diagnosis.
                            error=(str(second_err) if str(second_err).startswith('ROW_FALLBACK_')
                                   else 'ROW_FALLBACK_'+str(second_err))
                            ui_probe_v2(tab,context,backup,name,error)
                blocked=limit_state['status']
                if blocked is not None:
                    if file is not None:
                        file.unlink(missing_ok=True)
                        file=None
                    if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                        stop='SECURITY_RESTRICTION'
                        break
                    if blocked != 429:
                        stop='HTTP_ACCESS_RESTRICTION_'+str(blocked)
                        break
                    if limit_state['source'] not in ('library_page','library_popup'):
                        stop='HTTP_429_FROM_OTHER_TAB_OR_UNKNOWN_ORIGIN'
                        log_rate_limit(backup,name,'STOP_429_FROM_OTHER_TAB_OR_UNKNOWN')
                        print('  → HTTP 429 пришёл не из вкладки загрузки Library; '
                              'автоматический повтор отменён.')
                        break
                    if retries_429 >= args.max_429_retries:
                        stop='HTTP_ACCESS_RESTRICTION_429'
                        log_rate_limit(backup,name,'STOP_REPEATED_429',server_retry_after=limit_state['retry_after'])
                        print('  → Повторный/неразрешённый HTTP 429: очередь сохранена, автоматизация остановлена.')
                        break
                    wait_sec=cooldown_seconds(args.retry_429_after,limit_state['retry_after'])
                    if wait_sec>MAX_AUTOMATIC_COOLDOWN:
                        stop='SERVER_RETRY_AFTER_TOO_LONG'
                        log_rate_limit(backup,name,'STOP_RETRY_AFTER_TOO_LONG',server_retry_after=limit_state['retry_after'])
                        print(f'  → Retry-After требует {wait_sec:.0f} сек.; '
                              'автоматический повтор отменён (не сокращаем срок сервера).')
                        break
                    log_rate_limit(backup,name,'WAIT_THEN_RETRY_ONCE',wait_sec,limit_state['retry_after'])
                    print(f'  → HTTP 429: пауза {wait_sec:.0f} сек., затем ОДИН повтор этого файла. '
                          'При новом 429 — остановка.',flush=True)
                    # Deliberately leave this record PENDING in the on-disk queue.
                    report(report_file,selected)
                    time.sleep(wait_sec)
                    if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                        stop='SECURITY_RESTRICTION'
                        break
                    # We have waited. Re-open only the ordinary visible Library;
                    # do not invoke hidden endpoints or modify browser security state.
                    limit_state.update(status=None,retry_after=None,source=None,path_class=None)
                    retries_429+=1
                    try:
                        tab.goto(BASE,wait_until='domcontentloaded',timeout=60000)
                        search,reason=wait_library_search(tab,args.ui_wait)
                    except Exception as exc:
                        stop='LIBRARY_REOPEN_FAILED_'+type(exc).__name__
                        break
                    if limit_state['status'] is not None:
                        stop='HTTP_ACCESS_RESTRICTION_'+str(limit_state['status'])
                        break
                    if search is None:
                        stop=reason
                        ui_probe(tab,backup,reason)
                        break
                    print(f'  → Повтор после паузы: {name}',flush=True)
                    continue
                if restriction_visible_in_context(context, own_page=tab) or access_monitor.visible_warning():
                    if file is not None:
                        file.unlink(missing_ok=True)
                        file=None
                    stop='SECURITY_RESTRICTION'
                break
            if stop:
                # Do not persist a misleading NOT_FOUND or UI_ERROR for a
                # file interrupted by a rate limit; it must stay PENDING.
                report(report_file,selected)
                break
            if error=='SECURITY_CHECK':
                # Access protection is not a file result. Keep it PENDING.
                stop='SECURITY_CHECK'
                report(report_file,selected)
                break
            if error:
                for t in needed:
                    t['status']=error
                    t['note']='Поиск через Library UI; не доказательство удаления оригинала'
                print('  →',error)
                report(report_file,selected)
                if error in ('DOWNLOAD_BUTTON_NOT_FOUND','NO_BROWSER_DOWNLOAD_EVENT',
                             'DOWNLOAD_SUBMENU_UNRECOGNIZED','NO_BROWSER_DOWNLOAD_EVENT_AFTER_MENU',
                             'AMBIGUOUS_BROWSER_DOWNLOAD_EVENTS','BROWSER_DOWNLOAD_FAILED') or error.startswith(('UI_ERROR_', 'ROW_FALLBACK_')):
                    ui_probe_v2(tab,context,backup,name,error)
                    consecutive_ui_failures += 1
                    print(f'  → UI-ошибка {consecutive_ui_failures} подряд: файл отложен, перехожу к следующему имени.')
                    if consecutive_ui_failures>=5:
                        stop='LIBRARY_UI_FAILURE_LIMIT';break
                else:
                    consecutive_ui_failures=0
                if error=='SECURITY_CHECK':
                    stop='SECURITY_CHECK';break
                continue
            if file is None:
                stop='UNEXPECTED_EMPTY_DOWNLOAD';break
            size=file.stat().st_size
            content_type=downloaded_content_type(file)
            if content_type in ('HTML_ERROR_PAGE','JSON_ERROR_RESPONSE'):
                for t in needed:
                    t['status']='ERROR_RESPONSE_NOT_FILE'
                    t['note']=(f'Браузер получил {size} байт, тип {content_type}; '
                               'вместо оригинального Knowledge пришла страница/ответ ошибки')
                file.unlink(missing_ok=True)
                print(f'  → ERROR_RESPONSE_NOT_FILE: {content_type}; {size} байт')
                report(report_file,selected)
                continue
            if size!=needed[0]['size']:
                try:
                    meta=quarantine_mismatched_download(file,needed[0],backup)
                except Exception as exc:
                    print('  → Не удалось сохранить карантин:',type(exc).__name__)
                    # Keep the temp file if possible, for user-side review.
                    stop='QUARANTINE_WRITE_FAILED'
                    report(report_file,selected)
                    break
                for t in needed:
                    t['status']='WRONG_SIZE'
                    t['note']=(f'Скачано {size} байт, ожидалось {t["size"]}; '
                               f'SHA256={meta["received_sha256"]}; '
                               f'карантин={meta["saved_as"]}; НЕ оригинал')
                print(f'  → WRONG_SIZE: получено {size} байт, ожидалось {needed[0]["size"]} байт')
                print('    Отдельно сохранено для анализа:',meta['saved_as'])
                print('    В Knowledge не помещено; статус сохранён в журнале.')
                report(report_file,selected)
                continue
            consecutive_ui_failures=0
            for t in needed:
                st,candidate_path,digest=stage_candidate(file,t,backup)
                t['status']='LIBRARY_CANDIDATE_STAGED'
                if st=='STAGED':
                    new_count+=1
                t['sha256']=digest
                t['candidate_path']=candidate_path
                t['note']='Library CANDIDATE ONLY, NOT GPT ORIGINAL; path='+candidate_path
            file.unlink(missing_ok=True)
            print('  → Сохранено ОТДЕЛЬНЫХ КАНДИДАТОВ Library (НЕ оригиналов GPT):',new_count)
            report(report_file,selected)
        if stop:print('Остановка:',stop)
        print(f'Результат: поисков {searches}; новых файлов {new_count}.')
        print('Журнал:',report_file)
        return 0 if stop in (None,'SEARCH_LIMIT','DOWNLOAD_LIMIT') else 3
    except KeyboardInterrupt:
        print('Остановлено пользователем; очередь не потеряна.')
        report(report_file,selected)
        return 130
    except Exception as exc:
        print('Остановлено:',type(exc).__name__,str(exc))
        report(report_file,selected)
        return 2
    finally:
        if access_monitor is not None:
            try:access_monitor.close()
            except Exception:pass
        if tab is not None:
            try:tab.close()
            except Exception:pass
        if p is not None:
            try:p.stop()
            except Exception:pass


if __name__=='__main__':sys.exit(main())
