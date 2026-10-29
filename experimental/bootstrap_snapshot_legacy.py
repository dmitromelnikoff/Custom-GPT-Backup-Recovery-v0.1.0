#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LOCAL, READ-ONLY, BEST-EFFORT backup of *owned* ChatGPT Custom GPTs.

- Opens a separate Chromium/Chrome browser profile. You sign in yourself.
- Reads list and detail via read-only, UNDOCUMENTED ChatGPT web endpoints.
- Exports JSON, Instructions, named Knowledge files where downloads are allowed,
  action-related configuration (where present in source responses), and an audit CSV.
- No POST/PATCH/PUT/DELETE, no GPT migrations, no uploads, and no secrets printed.
- Never saves the login access token. Browser profile DOES contain login cookies.
- Some published/draft state or file/action data may not be provided by endpoints.

Requirements: python -m pip install playwright
              python -m playwright install chromium
Usage:        python bootstrap_snapshot_legacy.py --normal-chrome
              python bootstrap_snapshot_legacy.py
              python bootstrap_snapshot_legacy.py --limit 2
              python bootstrap_snapshot_legacy.py --limit 2
              python bootstrap_snapshot_legacy.py --no-files
              python bootstrap_snapshot_legacy.py --force
"""
from __future__ import annotations
import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
import traceback
import os
import shutil
import socket
import subprocess
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlsplit

URL_ROOT = "https://chatgpt.com"
LIST_URL = URL_ROOT + "/public-api/gizmos/discovery/mine"
RECOVERABLE_CODES = {429, 500, 502, 503, 504}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def safe_name(s: str, max_len=68) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:max_len].rstrip(" .") or "unnamed")


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def unwrap(item):
    """Known historical shapes of user-owned gizmo list/detail responses."""
    obj = item
    for _ in range(5):
        if not isinstance(obj, dict):
            return {}
        if isinstance(obj.get("id"), str) and obj["id"].startswith("g-"):
            return obj
        next_obj = None
        if isinstance(obj.get("resource"), dict):
            next_obj = obj["resource"]
        elif isinstance(obj.get("gizmo"), dict):
            next_obj = obj["gizmo"]
        if next_obj is None:
            return obj
        obj = next_obj
    return obj if isinstance(obj, dict) else {}


def as_name(g):
    d = g.get("display") or {}
    if not isinstance(d, dict):
        d = {}
    return d.get("name") or g.get("name") or g.get("id") or "untitled"


def first_instructions(*responses):
    """Read actual instructions string from GPT payload; no fake reconstruction."""
    for resp in responses:
        for node in [unwrap(resp), resp]:
            if isinstance(node, dict) and isinstance(node.get("instructions"), str) and node["instructions"].strip():
                return node["instructions"]
            if isinstance(node, dict) and isinstance(node.get("gizmo"), dict) and isinstance(node["gizmo"].get("instructions"), str) and node["gizmo"]["instructions"].strip():
                return node["gizmo"]["instructions"]
    return None


def data_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    lst = payload.get("list")
    if isinstance(lst, dict) and isinstance(lst.get("items"), list):
        return lst["items"]
    for k in ("items", "gizmos", "results"):
        if isinstance(payload.get(k), list):
            return payload[k]
    return []


def data_cursor(payload):
    if not isinstance(payload, dict):
        return None
    lst = payload.get("list")
    if isinstance(lst, dict) and lst.get("cursor"):
        return lst["cursor"]
    return payload.get("cursor") or payload.get("next_cursor") or None


def discover_files(payload):
    """File metadata known to belong to this GPT, not unrelated global files."""
    out, visited = [], set()
    keys = {"files", "knowledge_files", "attachments", "reference_files"}
    def walk(x, path="root", depth=0):
        if depth > 8 or not isinstance(x, (dict, list)):
            return
        if id(x) in visited:
            return
        visited.add(id(x))
        if isinstance(x, dict):
            for k, v in x.items():
                if k.lower() in keys and isinstance(v, list):
                    for f in v:
                        if not isinstance(f, dict):
                            continue
                        file_id = f.get("file_id") or f.get("fileId") or f.get("id")
                        if not isinstance(file_id, str):
                            continue
                        # Accept file ids from explicit Knowledge/file collections;
                        # do not try unrelated avatar/image assets.
                        out.append({"file_id": file_id, "name": f.get("name") or f.get("filename") or f.get("file_name") or file_id,
                                    "type": f.get("type") or f.get("mime_type"), "size": f.get("size"),
                                    "source_path": path + "." + k})
                if isinstance(v, (dict, list)):
                    walk(v, path + "." + str(k), depth + 1)
        else:
            for idx, v in enumerate(x):
                if isinstance(v, (dict, list)):
                    walk(v, path + "[" + str(idx) + "]", depth + 1)
    walk(payload)
    dedup = {}
    for f in out:
        dedup.setdefault(f["file_id"], f)
    return list(dedup.values())


def find_action_configs(payload):
    """Retain only actual server-returned action-ish objects, never invent schemas."""
    out = []
    def walk(x, path="root", depth=0):
        if depth > 9 or len(out) > 250:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                lk = k.lower()
                if any(t in lk for t in ("action", "openapi", "api_spec", "api_schema")) and isinstance(v, (dict, list, str)):
                    if v and (not isinstance(v, str) or len(v) >= 10):
                        out.append({"path": path+"."+k, "value": v})
                # Tools often include action entries, not only action-prefixed fields.
                if lk == "tools" and isinstance(v, list):
                    for i, elem in enumerate(v):
                        if isinstance(elem, dict) and "action" in str(elem.get("type", "")).lower():
                            out.append({"path": f"{path}.tools[{i}]", "value": elem})
                if isinstance(v, (dict, list)):
                    walk(v, path+"."+k, depth+1)
        elif isinstance(x, list):
            for i, v in enumerate(x):
                if isinstance(v, (dict, list)):
                    walk(v, path+f"[{i}]", depth+1)
    walk(payload)
    return out


SECRET_KEYS = {"access_token", "refresh_token", "id_token", "session_token", "client_secret", "api_key", "apikey", "password", "private_key", "authorization", "cookie", "cookies", "bearer_token"}

def mask_known_secrets(obj, depth=0):
    # The returned config could contain auth fields; redact plain secrets by key.
    if depth > 30:
        return "<depth-limit>"
    if isinstance(obj, list):
        return [mask_known_secrets(v, depth+1) for v in obj]
    if isinstance(obj, dict):
        d = {}
        for key, val in obj.items():
            low = str(key).lower()
            if low.replace('-', '_') in SECRET_KEYS and isinstance(val, str) and val:
                d[key] = "[REDACTED_SECRET]"
            else:
                d[key] = mask_known_secrets(val, depth+1)
        return d
    return obj


FETCH_JS = """async ({url, token}) => {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 45000);
  try {
    const r = await fetch(url, {
      method: 'GET', credentials: 'include', cache: 'no-store',
      headers: {'Accept':'application/json', 'Authorization':'Bearer ' + token},
      signal: ctrl.signal
    });
    const text = await r.text();
    let data = null;
    try { data = JSON.parse(text); }
    catch { data = {'_unexpected_content_type':r.headers.get('content-type'), '_body_prefix': text.slice(0,300)}; }
    return {status:r.status, data};
  } catch (e) { return {status:0, data:{error:String(e)}}; }
  finally { clearTimeout(timer); }
}"""

SESSION_JS = """async () => {
  const r = await fetch('/api/auth/session', {credentials:'include',cache:'no-store'});
  if (!r.ok) return null;
  const j = await r.json();
  return j.accessToken || j.access_token || null;
}"""


def request_json(page, url, token, label, tries=4):
    for i in range(tries):
        response = page.evaluate(FETCH_JS, {"url":url,"token":token})
        status = response.get("status",0)
        if status == 200:
            return response["data"], 200
        if status in RECOVERABLE_CODES and i < tries-1:
            pause = min(30, 2 ** i * 2)
            print(f"  {label}: HTTP {status}, повтор через {pause} с")
            time.sleep(pause)
            continue
        return response.get("data"), status
    return {}, 0


def check_signed_url(url):
    u = urlsplit(url)
    return u.scheme == 'https' and bool(u.hostname) and not (u.username or u.password)


def download_signed(url, target, max_bytes=512 * 1024 * 1024):
    if not check_signed_url(url):
        raise ValueError("Некорректная HTTPS-ссылка для скачивания")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.part')
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, open(temp,'wb') as output:
            if int(resp.headers.get("Content-Length", "0") or "0") > max_bytes:
                raise ValueError("Файл превышает лимит 512 МБ")
            total = 0
            while True:
                block = resp.read(256 * 1024)
                if not block: break
                total += len(block)
                if total > max_bytes:
                    raise ValueError("Файл превышает лимит 512 МБ")
                output.write(block)
            if total == 0:
                raise ValueError("Сервер вернул пустой файл")
        temp.replace(target)
        return total
    finally:
        if temp.exists():
            temp.unlink()


def export_knowledge(page, raw, gid, dest:Path, token):
    files = discover_files(raw)
    result=[]
    for n, f in enumerate(files,1):
        fid=f["file_id"]
        outfile=dest / f"{n:02d}_{safe_name(f['name'],110)}"
        item={**f, "download_status":"not_attempted", "local_file":str(outfile.name)}
        try:
            if outfile.is_file() and outfile.stat().st_size > 0:
                item["download_status"]="already_exists"
                item["bytes"] = outfile.stat().st_size
            else:
                q = urlencode({"gizmo_id":gid,"inline":"false"})
                api_url = URL_ROOT+"/backend-api/files/download/"+fid+"?"+q
                data, code=request_json(page, api_url, token, "Knowledge " +str(n), tries=2)
                # No guessed access. A valid, signed download_url must be returned.
                signed = (data.get("download_url") or data.get("url")) if isinstance(data,dict) else None
                if code == 200 and isinstance(signed,str) and check_signed_url(signed):
                    item["bytes"] = download_signed(signed, outfile)
                    item["download_status"]="downloaded"
                else:
                    item["download_status"]="unavailable"
                    item["http_status"]=code
                    item["reason"]="Сервер не предоставил подтверждённую ссылку на файл"
        except Exception as e:
            item["download_status"]="error"
            item["reason"] = str(e)[:300]
        result.append(item)
        time.sleep(0.35)
    return result


def enumerate_gpts(page, token, root):
    all_items={}
    cursor=None
    seen_cursors=set()
    for page_num in range(1,100):
        qs={"limit":20}
        if cursor:
            qs["cursor"]=cursor
        url=LIST_URL+"?"+urlencode(qs)
        data,code=request_json(page,url,token,"Список GPT, стр. " +str(page_num))
        if code != 200:
            raise RuntimeError(f"Невозможно прочитать список Custom GPT: HTTP {code}, {str(data)[:160]}")
        entries=data_items(data)
        for item in entries:
            g = unwrap(item)
            gid=g.get("id") if isinstance(g,dict) else None
            if isinstance(gid,str) and gid.startswith('g-') and not gid.startswith('g-p-'):
                all_items[gid] = item
        print(f"  Страница {page_num}: получено {len(entries)}, всего уникальных {len(all_items)}")
        cursor=data_cursor(data)
        if not cursor or not entries:
            break
        if cursor in seen_cursors:
            raise RuntimeError("Обнаружен повторяющийся cursor, остановка")
        seen_cursors.add(cursor)
        time.sleep(0.6)
    save_json(root/"gpt_list_snapshot.json", mask_known_secrets({"created_utc":now(),"gpts":list(all_items.values()),"count":len(all_items)}))
    if not all_items:
        raise RuntimeError("Список GPT пуст. Возможно, авторизация недоступна или изменился формат API.")
    return all_items



def locate_chrome():
    """Find a normal Google Chrome installation without relying on Playwright."""
    guesses = [
        shutil.which('google-chrome'), shutil.which('google-chrome-stable'),
        shutil.which('chrome'), shutil.which('chromium'),
        shutil.which('chromium-browser'),
    ]
    if sys.platform == 'win32':
        for env, suffix in (
            ('PROGRAMFILES', 'Google/Chrome/Application/chrome.exe'),
            ('PROGRAMFILES(X86)', 'Google/Chrome/Application/chrome.exe'),
            ('LOCALAPPDATA', 'Google/Chrome/Application/chrome.exe'),
        ):
            if os.environ.get(env):
                guesses.append(str(Path(os.environ[env]) / suffix))
    elif sys.platform == 'darwin':
        guesses += [
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
            str(Path.home()/'Applications/Google Chrome.app/Contents/MacOS/Google Chrome'),
        ]
    return next((str(p) for p in guesses if p and Path(p).is_file()), None)


def start_normal_chrome(profile: Path):
    """Launch Chrome normally; optionally attach later over a loopback-only CDP port.

    Warning: DevTools remote debugging can access browser contents; do not expose
    this port and close the launched browser when the backup completes.
    """
    exe = locate_chrome()
    if not exe:
        raise RuntimeError('Не найден Google Chrome. Установи Chrome или используй прежний режим без --normal-chrome.')
    profile.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    cmd = [exe,
           '--remote-debugging-address=127.0.0.1',
           f'--remote-debugging-port={port}',
           f'--user-data-dir={str(profile)}',
           '--no-first-run',
           'https://chatgpt.com/auth/login']
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print('Открывается обычный Google Chrome с отдельным профилем.')
    print('В нём пройди стандартный вход в ChatGPT. Не передавай никому профиль браузера.')
    print('Если видишь только логотип, попробуй Ctrl+Shift+R, затем https://chatgpt.com/auth/login')
    print('Порт отладки доступен только на 127.0.0.1. Закрой это окно Chrome после экспорта.')
    input('Когда откроется твой аккаунт ChatGPT, нажми ENTER здесь... ')
    return port

def main():
    ap=argparse.ArgumentParser(description="Локальная резервная копия собственных Custom GPT")
    ap.add_argument('--out',default='Custom_GPT_backup',help='Папка для резервной копии')
    ap.add_argument('--profile',default='.chatgpt_backup_chrome_profile',help='Папка авторизованного браузера (хранить в секрете)')
    ap.add_argument('--limit',type=int,default=0,help='Максимум GPT для пробного запуска, 0=все')
    ap.add_argument('--ids',nargs='+',default=None,help='Проверить только перечисленные GPT ID через пробел')
    ap.add_argument('--no-files',action='store_true',help='Не пытаться скачивать Knowledge, сохранить только метаданные')
    ap.add_argument('--force',action='store_true',help='Повторно получить данные уже сохранённых GPT')
    ap.add_argument('--builtin-browser',action='store_true',help='Использовать установленный Playwright Chromium вместо Google Chrome')
    ap.add_argument('--normal-chrome',action='store_true',help='Войти через обычный Chrome и подключиться после входа (альтернатива зависшему окну Playwright)')
    args=ap.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit('Установи Playwright: python -m pip install playwright; python -m playwright install chromium')
    root=Path(args.out).expanduser().absolute()
    profile_name=('.chatgpt_backup_normal_chrome_profile' if args.normal_chrome and args.profile == '.chatgpt_backup_chrome_profile' else args.profile)
    profile=Path(profile_name).expanduser().absolute()
    root.mkdir(parents=True,exist_ok=True)
    print('ЛОКАЛЬНЫЙ ЭКСПОРТ. Режим только чтения — ничего в GPT не меняет.')
    print('Данные сохраняются локально:',root)
    print('Профиль браузера содержит авторизацию; не публикуй эту папку:',profile)
    print('Интерфейсы неофициальные. Бинарные Knowledge-файлы и Actions могут быть недоступны.')
    with sync_playwright() as p:
        remote_browser = None
        if args.normal_chrome:
            port=start_normal_chrome(profile)
            try:
                remote_browser=p.chromium.connect_over_cdp(f'http://127.0.0.1:{port}',timeout=20000)
            except Exception as e:
                sys.exit('Не удалось подключиться к обычному Chrome. Убедись, что окно осталось открытым.\n'+str(e))
            if not remote_browser.contexts:
                sys.exit('Chrome не предоставил доступный браузерный контекст.')
            browser=remote_browser.contexts[0]
            print('Подключено к обычному Chrome. Проверяю авторизацию...')
        else:
            options={"headless":False,"accept_downloads":False,"viewport":{"width":1280,"height":900}}
            if not args.builtin_browser: options['channel']='chrome'
            try:
                browser=p.chromium.launch_persistent_context(str(profile),**options)
            except Exception as e:
                if args.builtin_browser:
                    raise
                sys.exit('Google Chrome не запустился. Попробуй --builtin-browser после: python -m playwright install chromium\n'+str(e))
        try:
            page=next((x for x in browser.pages if x.url.startswith('https://chatgpt.com/')),
                      browser.pages[0] if browser.pages else browser.new_page())
            if not args.normal_chrome:
                page.goto('https://chatgpt.com/gpts/mine',wait_until='domcontentloaded',timeout=60000)
                print('\nВ открывшемся окне войди в СВОЙ ChatGPT, если нужно.')
                print('Если только логотип OpenAI, попробуй Ctrl+L -> https://chatgpt.com/auth/login -> ENTER и Ctrl+Shift+R.')
                input('После входа в браузере нажми ENTER в этом терминале... ')
            page.goto('https://chatgpt.com/gpts/mine',wait_until='domcontentloaded',timeout=60000)
            token=page.evaluate(SESSION_JS)
            if not token:
                sys.exit('Не получен токен активной сессии. Проверь вход на chatgpt.com в открывшемся браузере.')
            # token is kept only in memory, never printed or persisted.
            all_items=enumerate_gpts(page,token,root)
            if args.ids:
                items=[(gid,all_items[gid]) for gid in args.ids if gid in all_items]
                missing=[gid for gid in args.ids if gid not in all_items]
                if missing:
                    print('ВНИМАНИЕ! Нет в списке доступных GPT:',', '.join(missing))
            elif args.limit > 0:
                items=list(all_items.items())[:args.limit]
            else:
                items=list(all_items.items())
            print(f'\nК обработке: {len(items)} GPT. Сохранение выполняется после каждого GPT.')
            results=[]
            for idx,(gid,item) in enumerate(items,1):
                summary=unwrap(item)
                name=as_name(summary)
                folder=root/'gpts'/safe_name(name)/safe_name(gid,90)
                folder.mkdir(parents=True,exist_ok=True)
                statusfile=folder/'backup_status.json'
                if statusfile.exists() and not args.force:
                    try:
                        existing=json.loads(statusfile.read_text(encoding='utf-8'))
                        if existing.get('finished'):
                            print(f'[{idx}/{len(items)}] SKIP готово: {name}')
                            results.append(existing)
                            continue
                    except Exception:
                        pass
                print(f'[{idx}/{len(items)}] {name} ({gid})')
                try:
                    detail=None
                    source='list_only'
                    detail_errors=[]
                    for url in (URL_ROOT+'/backend-api/gizmos/'+gid,
                                URL_ROOT+'/public-api/gizmos/'+gid):
                        payload,code=request_json(page,url,token,'Детали GPT',tries=2)
                        if code == 200 and isinstance(payload,dict):
                            g=unwrap(payload)
                            if g.get('id') == gid:
                                detail=payload
                                source=url.split('/')[3]
                                break
                            detail_errors.append(f'{url.split("/")[3]}: неожиданный ID/структура')
                        else:
                            detail_errors.append(f'{url.split("/")[3]}: HTTP {code}')
                    # Preserve original list/detail source representations for audit.
                    save_json(folder/'list_record.json',mask_known_secrets(item))
                    if detail is not None:
                        save_json(folder/'detail_record.json',mask_known_secrets(detail))
                    instr=first_instructions(detail or {},item)
                    if instr is not None:
                        (folder/'instructions.md').write_text(instr,encoding='utf-8')
                    raw=detail if detail is not None else item
                    actions=find_action_configs(raw)
                    save_json(folder/'actions_detected.json',mask_known_secrets({"note":"Найденные поля могут не представлять все Actions. Отсутствие полей не доказывает отсутствие Actions.","items":actions}))
                    resource_files=discover_files(raw)
                    if args.no_files:
                        knowledge=[{**f,"download_status":"not_attempted (--no-files)"} for f in resource_files]
                    else:
                        knowledge=export_knowledge(page,raw,gid,folder/'knowledge',token)
                    save_json(folder/'knowledge_manifest.json',knowledge)
                    record={"gpt_id":gid,"name":name,"source":source,"snapshot_utc":now(),
                            "instructions_present":instr is not None,"instructions_chars":len(instr or ''),
                            "knowledge_items_detected":len(resource_files),
                            "knowledge_downloaded":sum(f['download_status'] in ('downloaded','already_exists') for f in knowledge),
                            "actions_fields_detected":len(actions),"details_errors":detail_errors,
                            "finished":True,
                            "verification":"UNVERIFIED CURRENT/DRAFT: these are web API read responses, not an official certified export"}
                    save_json(statusfile,record)
                    results.append(record)
                except KeyboardInterrupt:
                    print('\nОстановлено пользователем. Уже сохранённые GPT останутся на диске.')
                    raise
                except Exception as e:
                    print('  Ошибка:',e)
                    record={"gpt_id":gid,"name":name,"finished":False,"error":str(e)[:250]}
                    save_json(statusfile,record)
                    results.append(record)
                time.sleep(0.7)
            keys=['gpt_id','name','finished','source','instructions_present','instructions_chars','knowledge_items_detected','knowledge_downloaded','actions_fields_detected','error']
            with (root/'inventory.csv').open('w',newline='',encoding='utf-8-sig') as csvfile:
                writer=csv.DictWriter(csvfile,fieldnames=keys,extrasaction='ignore',delimiter=';')
                writer.writeheader()
                writer.writerows(results)
            print('\nГОТОВО. Отчёт:', root/'inventory.csv')
            print('Обработано записей:',len(results),'| Instructions найдено:',sum(bool(r.get('instructions_present')) for r in results),
                  '| Knowledge-файлов скачано:',sum(r.get('knowledge_downloaded',0) for r in results))
            print('ВАЖНО: Содержимое папки конфиденциально. Перед отправкой материалов проверь Actions и данные.')
        finally:
            if remote_browser is not None:
                remote_browser.close()  # disconnect; leave Chrome window for user to close
            else:
                browser.close()


if __name__=='__main__':
    try: main()
    except KeyboardInterrupt:
        sys.exit(130)
