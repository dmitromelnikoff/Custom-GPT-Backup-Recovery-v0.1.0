#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backup ORIGINAL Knowledge through the owner's VISIBLE Custom GPT editor.

V11 unattended queue runner: process all eligible owned GPTs, safely defer per-file UI failures, keep provenance and audit trail.
Based on the real-account-tested V10/V9/V5 direct download mechanism.
V7 404/error deferrals remain; V8 adds strict receipt-backed donor candidates and shared priority; exact-file selection, safe preview handling,
size/hash verification and browser isolation are inherited from v5.
Never clicks Delete or anonymous controls.
No backend APIs, auth token access, plugin sources, GPT modification or uploads.
Reads GPT IDs/names/sizes only from the owner's existing snapshot.
Connects ONLY to an already-open loopback-debuggable Chrome session.

The GPT editor UI can change. On unknown controls, save privacy-limited diagnostics
and defer the file; never click unrelated settings or delete files.

Requires: pip install playwright psutil
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_GPT = ''  # No account-specific GPT is hard-coded in public release
EDITOR = 'https://chatgpt.com/gpts/editor/{}'
MAX_BYTES = 512 * 1024 * 1024
STOP_CODES = {401, 403, 429}


def safe_part(s, limit=100):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s))
    s = re.sub(r'\s+', ' ', s).strip(' .')[:limit].rstrip(' .') or 'unnamed'
    if s.partition('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL'}:
        s = '_' + s
    return s


def norm(s):
    return unicodedata.normalize('NFC', str(s)).casefold().strip()


def sha256_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def parse_gpt(snapshot, backup, gpt_id, blob=None):
    if blob is None:
        blob = json.loads(snapshot.read_text(encoding='utf-8-sig'))
    for entry in blob.get('gpts', []) if isinstance(blob, dict) else blob:
        r = entry.get('resource', entry)
        g = r.get('gizmo', r)
        if g.get('id') != gpt_id:
            continue
        name = (g.get('display') or {}).get('name') or gpt_id
        folder = backup / 'gpts' / safe_part(name, 68) / safe_part(gpt_id, 90) / 'knowledge'
        targets=[]
        for index, f in enumerate(r.get('files') or [], 1):
            fn, size = f.get('name'), f.get('size')
            if not isinstance(fn, str) or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
                raise ValueError('Некорректное имя/размер Knowledge в snapshot: '+str(f))
            targets.append({'gpt_id':gpt_id, 'gpt_name':name,'file_id':f.get('file_id') or '',
                            'name':fn, 'expected':size, 'ordinal':index,
                            'dest':folder / f'{index:02d}_{safe_part(fn)}'})
        return name, targets
    raise ValueError('GPT не найден в snapshot: '+gpt_id)


def existing_chrome_port(profile: Path, specified):
    import psutil
    def valid(port):
        try:
            port=int(port)
            if not 1 <= port <= 65535: return False
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/json/version',timeout=2) as resp:
                data=json.load(resp)
            return ('Chrome/' in data.get('Browser','') or 'Chromium/' in data.get('Browser',''))
        except Exception: return False
    if specified is not None:
        if valid(specified): return specified
        raise RuntimeError('Локальный CDP-порт не отвечает как Chrome.')
    portfile=profile / 'DevToolsActivePort'
    if portfile.is_file():
        try:
            port=int(portfile.read_text('utf-8').splitlines()[0])
            if valid(port): return port
        except Exception: pass
    desired=os.path.normcase(os.path.abspath(str(profile)))
    for proc in psutil.process_iter(attrs=['cmdline']):
        try:
            argv=proc.info['cmdline'] or []
            args={}
            for i,val in enumerate(argv):
                for key in ('--remote-debugging-port', '--user-data-dir'):
                    if val.startswith(key+'='): args[key]=val.split('=',1)[1]
                    elif val==key and i+1<len(argv): args[key]=argv[i+1]
            candidate=args.get('--remote-debugging-port')
            dirname=args.get('--user-data-dir')
            if candidate and dirname and os.path.normcase(os.path.abspath(dirname.strip('"'))) == desired and valid(candidate):
                return int(candidate)
        except Exception: pass
    raise RuntimeError('Не найден уже авторизованный Chrome с DevTools и ожидаемым профилем. '
                       'Проверь, что окно Chrome от старых скриптов осталось открытым.')


def ui_access_warning(page):
    if page.is_closed(): return False
    try:
        if re.search(r'/auth/login|/login|/auth/verify|challenge',page.url,re.I):return True
        # Only check obvious access warning phrases; ignore normal error notices.
        return page.get_by_text(re.compile(r'слишком много запросов|too many requests|'
                                           r'доступ.*временно ограничен|access.*temporarily restricted',re.I)).count()>0
    except Exception:
        return False


def evidence_filename_visible(page, name):
    # Exact visible matching; do not use fuzzy substring or another document.
    try:
        nodes=page.get_by_text(name, exact=True)
        return [nodes.nth(i) for i in range(min(nodes.count(),30))
                if nodes.nth(i).is_visible()]
    except Exception: return []


def editor_probe(page, output, stage, expected_name='', expected_files=None):
    """Limited UI metadata: no HTML, instructions, profile info, cookies or signed URLs."""
    if page is None or page.is_closed():
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps({'stage':stage,'expected_file':expected_name,
            'editor_page_closed':True,
            'privacy_note':'No HTML, file text, instructions, request URLs or tokens.'},
            ensure_ascii=False,indent=2),encoding='utf-8')
        return output
    expected_files=expected_files or []
    snapshot={'stage':stage,'expected_file':expected_name,
              'editor_url_class':('/gpts/editor/' if '/gpts/editor/' in page.url else 'other'),
              'editor_visible_filename_count':0,'controls':[],'headings':[],
              'configuration_toggle_present':False,'configuration_selected':False,
              'knowledge_header_visible':False,
              'target_context_controls':[],
              'privacy_note':'Only visible control labels and headings. No HTML, instructions, request URLs or tokens.'}
    try:
        snapshot['editor_visible_filename_count']=sum(bool(evidence_filename_visible(page, n)) for n in expected_files)
        toggle,_=find_config_toggle(page)
        snapshot['configuration_toggle_present']=toggle is not None
        snapshot['configuration_selected']=bool(toggle is not None and is_config_selected(toggle))
        snapshot['knowledge_header_visible']=find_knowledge_header(page) is not None
        exact_node, _ = find_filename_node(page,expected_name) if expected_name else (None,None)
        matched=[exact_node] if exact_node is not None else (evidence_filename_visible(page,expected_name) if expected_name else [])
        if matched:
            node=matched[0]
            snapshot['target_structure']=file_row_details(node,expected_name,expected_files)
            kind,control,level=find_explicit_action(node,expected_name,expected_files)
            snapshot['target_detected_action']=kind or 'none'
            snapshot['target_detected_action_ancestor_level']=level
            snapshot['target_named_preview_button']=named_file_button(node,expected_name) is not None
            snapshot['exact_file_button_aria']=control_label(node) if named_file_button(node,expected_name) else ''
            for level in range(1,5):
                try:
                    parent=node.locator('xpath='+('/'.join(['..']*level)))
                    labels=[control_label(x) for x in visible_controls(parent)]
                    if labels and len(labels)<18:
                        snapshot['target_context_controls']=[x for x in labels if x][:18]
                        break
                except Exception:pass
        snapshot['controls']=page.evaluate('''() => {
          const out=[];
          const visible=(e)=>{const r=e.getBoundingClientRect();const s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
          for (const e of [...document.querySelectorAll('button,[role="button"],[role="tab"],[role="menuitem"],a[download]')]) {
            if (!visible(e))continue;
            const text=(e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,125);
            if(!text)continue;
            out.push({role:e.getAttribute('role')||e.tagName.toLowerCase(),label:text});
            if(out.length>=100)break;
          }return out;
        }''')
        snapshot['headings']=page.evaluate('''() => [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
           .filter(e=>{const r=e.getBoundingClientRect();return r.height>0&&r.width>0})
           .slice(0,35).map(e=>(e.innerText||'').trim().slice(0,100))''')
    except Exception as exc:
        snapshot['probe_error']=type(exc).__name__
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    return output


def find_config_toggle(page):
    """Russian UI exposes Configure as a RADIO, not as a tab or button."""
    names=('Конфигурация', 'Настроить', 'Configure', 'Configuration')
    for role in ('radio', 'tab', 'button'):
        for word in names:
            try:
                nodes=page.get_by_role(role,name=re.compile(r'^'+re.escape(word)+r'$', re.I))
                matches=[nodes.nth(i) for i in range(min(10,nodes.count())) if nodes.nth(i).is_visible()]
                if len(matches)==1:return matches[0],role
            except Exception:pass
    # Exact visible-text fallback for variants not exposing a role; do not click
    # by partial match, because this editor contains unrelated settings.
    for word in names:
        try:
            nodes=page.get_by_text(word,exact=True)
            matches=[nodes.nth(i) for i in range(min(10,nodes.count())) if nodes.nth(i).is_visible()]
            if len(matches)==1:return matches[0],'exact_text'
        except Exception:pass
    return None,None


def is_config_selected(toggle):
    try:
        aria=toggle.get_attribute('aria-checked')
        if aria is not None:return aria.lower()=='true'
    except Exception:pass
    try:
        return toggle.is_checked()
    except Exception:return False


def find_knowledge_header(page):
    # Knowledge may be lower down inside the left, scrollable Configure panel.
    # Exact match only to avoid inadvertently scrolling/clicking Instructions.
    for role in ('heading',):
        try:
            nodes=page.get_by_role(role,name=re.compile(r'^(?:Знания|Knowledge)$',re.I))
            for i in range(min(nodes.count(),8)):
                if nodes.nth(i).is_visible():return nodes.nth(i)
        except Exception:pass
    for word in ('Знания','Knowledge'):
        try:
            nodes=page.get_by_text(word,exact=True)
            matches=[nodes.nth(i) for i in range(min(nodes.count(),12)) if nodes.nth(i).is_visible()]
            if len(matches)==1:return matches[0]
        except Exception:pass
    return None


def reveal_knowledge(page):
    header=find_knowledge_header(page)
    if header is None:return False
    try:header.scroll_into_view_if_needed(timeout=3000)
    except Exception:pass
    return True


def open_config_if_needed(page, target_names):
    """Select Configure radio and await Knowledge, without changing GPT data."""
    deadline=time.monotonic()+15
    toggle=None;role=None
    while time.monotonic()<deadline:
        toggle,role=find_config_toggle(page)
        if toggle:break
        page.wait_for_timeout(250)
    if toggle is None:return 'CONFIG_TOGGLE_NOT_FOUND'
    if not is_config_selected(toggle):
        try:toggle.click(timeout=8000)
        except Exception as exc:return 'CONFIG_CLICK_FAILED_'+type(exc).__name__
    # Wait for the config panel to mount. It can take seconds on first load.
    deadline=time.monotonic()+25
    while time.monotonic()<deadline:
        if ui_access_warning(page):return 'ACCESS_RESTRICTION_IN_CONFIG'
        if reveal_knowledge(page):return 'CONFIG_KNOWLEDGE_VISIBLE'
        if any(evidence_filename_visible(page,n) for n in target_names):
            return 'CONFIG_FILENAMES_VISIBLE'
        page.wait_for_timeout(400)
    return 'CONFIG_OPEN_KNOWLEDGE_NOT_VISIBLE'


def find_filename_node(page, name):
    """Locate the actual named Knowledge button, not nearby text or Delete.

    Confirmed real editor UI: role=group aria-label=<filename> contains
    button aria-label=<filename> plus a separate button aria-label='Удалить файл: ...'.
    The previous get_by_text() picked non-action text and missed this button.
    """
    try:
        groups=page.get_by_role('group',name=name,exact=True)
        valid=[]
        for i in range(min(groups.count(),12)):
            group=groups.nth(i)
            if not group.is_visible():continue
            buttons=group.get_by_role('button',name=name,exact=True)
            for j in range(min(buttons.count(),12)):
                btn=buttons.nth(j)
                if btn.is_visible() and norm(control_label(btn))==norm(name):
                    valid.append(btn)
        if len(valid)==1:return valid[0],None
        if len(valid)>1:return None,'AMBIGUOUS_EXACT_KNOWLEDGE_BUTTON'

        # Fallback for editor variants that don't expose role=group.
        buttons=page.get_by_role('button',name=name,exact=True)
        valid=[]
        for i in range(min(buttons.count(),30)):
            btn=buttons.nth(i)
            if btn.is_visible() and norm(control_label(btn))==norm(name):
                valid.append(btn)
        if len(valid)==1:return valid[0],None
        if len(valid)>1:return None,'AMBIGUOUS_EXACT_KNOWLEDGE_BUTTON'
    except Exception:
        pass

    # Do not return a text node; clicking text has led to unreliable scrolling.
    return None,'EXACT_NAMED_KNOWLEDGE_BUTTON_NOT_FOUND'


def find_row(node, name, allnames):
    """Select a narrow ancestor containing THIS filename, not another Knowledge item."""
    try:
        for level in range(1,6):
            parent=node.locator('xpath='+('/'.join(['..'] * level)))
            if parent.count()!=1:continue
            if not parent.is_visible(): continue
            text=parent.inner_text(timeout=1200)
            if len(text)>1000:continue
            if any(other != name and other in text for other in allnames): continue
            ctrls=parent.locator('button,[role="button"],a[download],[role="menuitem"]')
            if 0 < ctrls.count() <= 9: return parent
    except Exception:pass
    return node


def visible_controls(scope):
    try:
        loc=scope.locator('button,[role="button"],[role="menuitem"],a[download]')
        return [loc.nth(i) for i in range(min(40,loc.count())) if loc.nth(i).is_visible()]
    except Exception:return []


def control_label(node):
    try:
        return node.evaluate('''e => (e.getAttribute('aria-label')||e.getAttribute('title')||e.innerText||'')
                       .trim().replace(/\\s+/g,' ').slice(0,200)''')
    except Exception:return ''


def direct_download_control(row, filename):
    choices=[]
    for el in visible_controls(row):
        label=control_label(el)
        if re.search(r'скачать|download',label,re.I):
            choices.append(el)
        else:
            try:
                if el.evaluate('(e)=>e.tagName==="A" && e.hasAttribute("download")'):
                    choices.append(el)
            except Exception:pass
    return choices[0] if len(choices)==1 else None


def menu_control(row, filename):
    opts=[]
    for el in visible_controls(row):
        label=control_label(el)
        if re.search(r'действия|actions|options|ещё|more|меню|menu|⋯|\.\.\.',label,re.I):opts.append(el)
    return opts[0] if len(opts)==1 else None


def download_item_from_menu(page):
    candidates=[]
    for el in visible_controls(page):
        label=control_label(el)
        if re.fullmatch(r'(?:Скачать|Download)(?:\s+файл|\s+file)?',label,re.I): candidates.append(el)
    menuitems=[el for el in candidates if (el.get_attribute('role') or '')=='menuitem']
    choices=menuitems if menuitems else candidates
    return choices[0] if len(choices)==1 else None


def wait_download_action(page, timeout_ms=4000):
    deadline=time.monotonic()+timeout_ms/1000
    while time.monotonic()<deadline:
        option=download_item_from_menu(page)
        if option:return option
        page.wait_for_timeout(150)
    return None


class ScopedDownloads:
    """Capture download events on ONLY editor tab and its own popups."""
    def __init__(self,page):
        self.context=page.context
        self.owned=set();self.callbacks=[];self.events=[]
        self.context.on('page',self._new_page)
        self.attach(page)
    def _new_page(self,p):
        try:
            if p.opener() in self.owned:self.attach(p)
        except Exception:pass
    def attach(self,page):
        if page in self.owned:return
        self.owned.add(page)
        page.on('download',self._download)
        page.on('popup',self.attach)
    def _download(self,event): self.events.append(event)
    def wait(self,timeout_ms):
        deadline=time.monotonic()+timeout_ms/1000
        while time.monotonic()<deadline:
            if self.events:return self.events.pop(0)
            active=next((pg for pg in self.owned if not pg.is_closed()),None)
            if active is None:return None
            try:active.wait_for_timeout(min(200, max(1, int((deadline-time.monotonic())*1000))))
            except Exception:return None
        return self.events.pop(0) if self.events else None
    def close(self):
        try:self.context.remove_listener('page',self._new_page)
        except Exception:pass
        for p in self.owned:
            try:p.remove_listener('download',self._download)
            except Exception:pass
            try:p.remove_listener('popup',self.attach)
            except Exception:pass


class AccessMonitor:
    """Only responses from this editor tab and its popups; never inspect other chats."""
    def __init__(self,page):
        self.status=None; self.sources=[]
        self.pages=set();self.context=page.context
        self.context.on('page',self._on_page)
        self.attach(page)
    def _on_page(self,page):
        try:
            if page.opener() in self.pages:self.attach(page)
        except Exception:pass
    def attach(self,page):
        if page in self.pages:return
        self.pages.add(page)
        page.on('response',self._on_response)
        page.on('popup',self.attach)
    def _on_response(self,response):
        try:
            from urllib.parse import urlsplit
            host=urlsplit(response.url).hostname
            if response.status in STOP_CODES and host in ('chatgpt.com','chat.openai.com'):
                self.status=response.status
        except Exception:pass
    def blocked(self):
        return self.status in STOP_CODES or any(ui_access_warning(p) for p in self.pages)
    def close(self):
        try:self.context.remove_listener('page',self._on_page)
        except Exception:pass
        for p in self.pages:
            try:p.remove_listener('response',self._on_response)
            except Exception:pass
            try:p.remove_listener('popup',self.attach)
            except Exception:pass


def scroll_target_once(node):
    """One direct scroll to the target, without returning to Knowledge heading."""
    try:
        node.evaluate("""e => e.scrollIntoView({block:'center', inline:'nearest', behavior:'instant'})""")
        return True
    except Exception:
        return False


def file_row_details(node, filename, allnames):
    """Privacy-limited structure: target ancestors and control labels, not Instructions/HTML."""
    try:
        return node.evaluate("""(e, args) => {
          const names = args.allnames || [];
          const visible = x => {
            const r=x.getBoundingClientRect(), s=getComputedStyle(x);
            return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
          };
          const brief = x => (x||'').trim().replace(/\\s+/g,' ').slice(0,140);
          const details=[];
          let p=e;
          for(let level=0;level<8 && p;level++,p=p.parentElement) {
            const texts=[...p.querySelectorAll('button,[role="button"],[role="menuitem"],a[download]')]
              .filter(visible).slice(0,20).map(x=>{
                const svg=x.querySelector('svg');
                return {
                  tag:x.tagName.toLowerCase(),
                  role:x.getAttribute('role')||'',
                  aria:brief(x.getAttribute('aria-label')||''),
                  title:brief(x.getAttribute('title')||''),
                  text:brief(x.innerText||''),
                  testid:brief(x.getAttribute('data-testid')||''),
                  svg_class:brief(svg?.getAttribute('class')||''),
                  svg_data_icon:brief(svg?.getAttribute('data-icon')||''),
                  svg_aria:brief(svg?.getAttribute('aria-label')||'')
                };
              });
            const full=p.textContent||'';
            const others=names.filter(n=>n!==args.filename && full.includes(n)).length;
            const rect=p.getBoundingClientRect();
            details.push({level,tag:p.tagName.toLowerCase(),role:p.getAttribute('role')||'',
              own_aria:brief(p.getAttribute('aria-label')||''),
              testid:brief(p.getAttribute('data-testid')||''),
              width:Math.round(rect.width),height:Math.round(rect.height),
              other_knowledge_filename_count:others,control_count:texts.length,controls:texts});
          }
          return details;
        }""", {'filename':filename,'allnames':list(allnames)})
    except Exception as exc:
        return [{'error':type(exc).__name__}]


def semantic_action(el):
    """Only explicitly labelled safe actions; anonymous X/trash is never clicked."""
    label=control_label(el)
    try:
        icons=el.evaluate("""e=>{
          const x=e.querySelector('svg');
          return x ? [x.getAttribute('class')||'',x.getAttribute('data-icon')||'',x.getAttribute('aria-label')||''].join(' ') : '';
        }""")
    except Exception:icons=''
    if re.search(r'скачать|download',label,re.I):return 'download'
    if re.search(r'действия|actions|options|ещ[её]|more|меню|menu|⋯|\.\.\.',label,re.I):return 'menu'
    if re.search(r'(^|[\s_\-])(?:download|arrow-down-to-line)(?:$|[\s_\-])',icons,re.I):return 'download'
    return None


def find_explicit_action(node, filename, names):
    """Choose the closest narrowly scoped ancestor with exactly one semantic action."""
    for level in range(0,8):
        try:
            parent=node if level==0 else node.locator('xpath='+'/'.join(['..']*level))
            if parent.count()!=1:continue
            if not parent.is_visible():continue
            # Do not risk a toolbar that also manages some OTHER Knowledge file.
            if level:
                contains_other=parent.evaluate("""(e, others) => {
                  const t=e.textContent||''; return others.some(n=>t.includes(n));
                }""",[n for n in names if n!=filename])
                if contains_other:continue
            controls=visible_controls(parent)
            if len(controls)>12:continue
            directs=[e for e in controls if semantic_action(e)=='download']
            menus=[e for e in controls if semantic_action(e)=='menu']
            if len(directs)==1:return 'download',directs[0],level
            if len(menus)==1:return 'menu',menus[0],level
            if len(directs)>1 or len(menus)>1:return 'ambiguous',None,level
        except Exception:pass
    return None,None,-1


def named_file_button(node, filename):
    """Accept only the exact, accessible filename button, never Delete."""
    try:
        if not node.is_visible():return None
        tag=node.evaluate('(e)=>e.tagName.toLowerCase()')
        if tag in ('button','a') and norm(control_label(node))==norm(filename):
            return node
        button=node.locator('xpath=ancestor-or-self::*[self::button or self::a or @role="button"][1]')
        if button.count()==1 and button.is_visible() and norm(control_label(button))==norm(filename):
            return button
    except Exception:pass
    return None


def _safe_page_class(url):
    """Do not persist arbitrary preview URLs, which may contain signed secrets."""
    try:
        from urllib.parse import urlsplit
        u=urlsplit(url)
        if not u.hostname:return 'local_or_empty'
        if '/gpts/editor/' in u.path:return 'gpt_editor'
        if '/library' in u.path:return 'library'
        if 'chatgpt.com' in u.hostname:return 'chatgpt_other'
        if 'openai.com' in u.hostname:return 'openai_other'
        return 'external_or_download'
    except Exception:return 'unknown'


def _probe_controls_in_scope(scope, max_count=85):
    """Only accessible control metadata; never text of GPT Instructions or file."""
    try:
        return scope.evaluate("""(limit) => {
          const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
            return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
          return [...document.querySelectorAll('button,[role="button"],[role="menuitem"],a[download]')]
           .filter(visible).slice(0,limit).map(e=>{
             const svg=e.querySelector('svg');
             return {
               tag:e.tagName.toLowerCase(),role:e.getAttribute('role')||'',
               aria:(e.getAttribute('aria-label')||'').slice(0,140),
               title:(e.getAttribute('title')||'').slice(0,140),
               text:(e.innerText||'').trim().replace(/\\s+/g,' ').slice(0,100),
               svg_class:(svg?.getAttribute('class')||'').slice(0,120),
               svg_data_icon:(svg?.getAttribute('data-icon')||'').slice(0,70),
               svg_lucide:(svg?.getAttribute('data-lucide')||'').slice(0,70)
             };
           });
        }""",max_count)
    except Exception as exc:
        return [{'error':type(exc).__name__}]


def _preview_scopes(editor_page, owned):
    """Editor page and its own popups/frames. Never inspect other chat tabs."""
    scopes=[]
    for p in [editor_page]+[x for x in owned if x != editor_page and not x.is_closed()]:
        if p.is_closed():continue
        try:
            for frame in p.frames:
                scopes.append((p,frame))
        except Exception:pass
    return scopes


def _wait_alive_owned_page(scope, timeout_ms=300):
    """Yield to Playwright on a LIVE owned page, even if editor closed on preview opening.

    Closing the editor tab during preview is a recoverable UI event, not automatically
    a missing Knowledge file. Never switch to unrelated user tabs.
    """
    alive=[]
    for p in list(scope.owned):
        try:
            if not p.is_closed():
                alive.append(p)
        except Exception:
            continue
    if not alive:
        return False
    # Prefer a newly opened child if the original editor has closed.
    for p in alive:
        try:
            p.wait_for_timeout(max(25,timeout_ms))
            return True
        except Exception:
            continue
    return False


def _visible_preview_downloads(editor_page, owned):
    """Find only download-labelled/recognizable icon controls in the preview."""
    choices=[]
    for owning_page, frame in _preview_scopes(editor_page,owned):
        try:
            nodes=frame.locator('button,[role="button"],[role="menuitem"],a[download]')
            for i in range(min(nodes.count(),140)):
                el=nodes.nth(i)
                if not el.is_visible():continue
                label=control_label(el)
                # Do not allow generic menus, delete, upload, or unrelated buttons.
                semantic=semantic_action(el)
                if re.fullmatch(r'\s*(?:Скачать|Download)(?:\s+(?:файл|file))?\s*',label,re.I):
                    choices.append((owning_page,el,'explicit'))
                elif semantic=='download':
                    choices.append((owning_page,el,'icon'))
        except Exception:pass
    # Dedupe locators returning the same DOM element from nested selectors.
    return choices


def inspect_preview_limited(editor_page,owned,expected_filename):
    """Metadata for exactly the controlled editor and child popups, not account tabs."""
    data={'stage':'PREVIEW_AFTER_EXACT_FILE_BUTTON',
          'expected_filename':expected_filename,
          'pages':[], 'closed_pages':0,
          'privacy_note':'No HTML, file text, instructions, cookies, tokens or full URLs.'}
    for p in [editor_page]+[x for x in owned if x!=editor_page]:
        try:
            if p.is_closed():
                data['closed_pages']+=1
                continue
            pageinfo={'page_type':_safe_page_class(p.url),'main_frames':len(p.frames),'frames':[]}
        except Exception:
            data['closed_pages']+=1
            continue
        for frame in p.frames[:12]:
            details={'frame_type':_safe_page_class(frame.url),
                     'controls':_probe_controls_in_scope(frame,max_count=65)}
            try:
                details['dialogs']=frame.locator('[role="dialog"]').count()
                details['menus']=frame.locator('[role="menu"]').count()
            except Exception:pass
            pageinfo['frames'].append(details)
        data['pages'].append(pageinfo)
    return data


def _write_preview_probe(path, editor_page, owned, expected_filename):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(inspect_preview_limited(editor_page,owned,expected_filename),
                               indent=2,ensure_ascii=False),encoding='utf-8')
    return path


def unique_preview_probe_path(backup, item):
    """Never overwrite previous UI diagnostics, including between different GPTs."""
    folder=backup/'Direct_GPT_UI_probes'/safe_part(item['gpt_id'],90)/safe_part(item['file_id'],90)
    folder.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
    return folder/(stamp+'_preview.json')


def _save_preview_after_run(probe_path, backup, item, reason):
    """If a preview probe was written, report its unique path in the journal."""
    if probe_path is not None and probe_path.is_file():
        print('    → Диагностика просмотрщика (отдельная для каждого файла):',probe_path)
        return ' | preview='+str(probe_path)
    return ''


def try_file_download(page, filename_node, filename, names, ms=25000,
                      preview_probe=None, access=None):
    """Click exact filename once. Inspect preview and its child frames before safe action."""
    stage='LOCATE_EXACT_FILE_BUTTON'
    scope=ScopedDownloads(page)
    try:
        button=named_file_button(filename_node,filename)
        if button is None:
            return None,'EXACT_NAMED_KNOWLEDGE_BUTTON_NOT_FOUND'
        stage='FIND_FILE_ROW_ACTION'
        kind,action,level=find_explicit_action(filename_node,filename,names)
        if kind=='ambiguous':return None,'AMBIGUOUS_FILE_ROW_ACTION'
        if access is not None and access.blocked():
            return None,'ACCESS_RESTRICTION_IN_EDITOR'
        if kind=='download':
            stage='CLICK_EXPLICIT_DOWNLOAD'
            action.click(timeout=5000)
            result=scope.wait(min(ms,6000))
            if result:return result,None
            stage='WAIT_SECOND_DOWNLOAD_MENU'
            option=wait_download_action(page,timeout_ms=3000)
            if option:
                if access is not None and access.blocked():return None,'ACCESS_RESTRICTION_IN_EDITOR'
                stage='CLICK_SECOND_MENU_DOWNLOAD'
                option.click(timeout=5000)
                result=scope.wait(ms)
                if result:return result,None
            return None,'NO_BROWSER_DOWNLOAD_EVENT_AFTER_EXPLICIT_DOWNLOAD'
        if kind=='menu':
            stage='CLICK_FILE_ACTIONS_MENU'
            action.click(timeout=5000)
            stage='FIND_MENU_DOWNLOAD'
            option=wait_download_action(page,timeout_ms=4000)
            if option is None:return None,'DOWNLOAD_NOT_PRESENT_IN_FILE_MENU'
            if access is not None and access.blocked():return None,'ACCESS_RESTRICTION_IN_EDITOR'
            stage='CLICK_MENU_DOWNLOAD'
            option.click(timeout=5000)
            result=scope.wait(ms)
            if result:return result,None
            return None,'NO_BROWSER_DOWNLOAD_EVENT_AFTER_FILE_MENU'

        stage='CLICK_EXACT_NAMED_KNOWLEDGE_BUTTON'
        button.click(timeout=7500)
        # A file click may either directly download or open a slow viewer.
        result=scope.wait(4500)
        if result:return result,None
        if access is not None and access.blocked():return None,'ACCESS_RESTRICTION_IN_PREVIEW'

        stage='FIND_PREVIEW_DOWNLOAD'
        end=time.monotonic()+15
        while time.monotonic()<end:
            if scope.events:return scope.events.pop(0),None
            if access is not None and access.blocked():return None,'ACCESS_RESTRICTION_IN_PREVIEW'
            candidates=_visible_preview_downloads(page,scope.owned)
            # If repeated downloads exist (editor + overlay), do not guess:
            # prefer one explicitly accessible Download action over icon guesses.
            exact=[el for owner,el,how in candidates if how=='explicit']
            icons=[el for owner,el,how in candidates if how=='icon']
            selected=exact[0] if len(exact)==1 else icons[0] if not exact and len(icons)==1 else None
            if selected is not None:
                stage='CLICK_PREVIEW_DOWNLOAD'
                selected.click(timeout=6000)
                result=scope.wait(5000)
                if result:return result,None
                if access is not None and access.blocked():return None,'ACCESS_RESTRICTION_IN_PREVIEW'
                # Download button may open another menu rather than download.
                stage='WAIT_NESTED_PREVIEW_DOWNLOAD'
                nested_end=time.monotonic()+5
                while time.monotonic()<nested_end:
                    if scope.events:return scope.events.pop(0),None
                    menu=[]
                    for owner, frame in _preview_scopes(page,scope.owned):
                        try:
                            loc=frame.locator('[role="menuitem"]')
                            for i in range(min(25,loc.count())):
                                e=loc.nth(i)
                                if e.is_visible() and re.fullmatch(r'\s*(Скачать|Download)\s*',control_label(e),re.I):
                                    menu.append(e)
                        except Exception:pass
                    if len(menu)==1:
                        if access is not None and access.blocked():return None,'ACCESS_RESTRICTION_IN_PREVIEW'
                        stage='CLICK_NESTED_PREVIEW_MENU_DOWNLOAD'
                        menu[0].click(timeout=6000)
                        result=scope.wait(ms)
                        if result:return result,None
                        break
                    if not _wait_alive_owned_page(scope,250):
                        if scope.events:return scope.events.pop(0),None
                        return None,'EDITOR_AND_VIEWER_PAGES_CLOSED'
                break
            if not _wait_alive_owned_page(scope,300):
                if scope.events:return scope.events.pop(0),None
                if preview_probe is not None:
                    try:_write_preview_probe(preview_probe,page,scope.owned,filename)
                    except Exception:pass
                return None,'EDITOR_AND_VIEWER_PAGES_CLOSED'

        if preview_probe is not None:
            _write_preview_probe(preview_probe,page,scope.owned,filename)
        if stage in ('CLICK_PREVIEW_DOWNLOAD','WAIT_NESTED_PREVIEW_DOWNLOAD',
                     'CLICK_NESTED_PREVIEW_MENU_DOWNLOAD'):
            return None,'NO_BROWSER_DOWNLOAD_EVENT_AFTER_PREVIEW'
        return None,'PREVIEW_DOWNLOAD_ACTION_NOT_VISIBLE'
    except Exception as exc:
        if preview_probe is not None:
            try:_write_preview_probe(preview_probe,page,scope.owned,filename)
            except Exception:pass
        if type(exc).__name__=='TargetClosedError':
            return None,'VIEWER_OR_EDITOR_CLOSED_AT_'+stage
        return None,'EDITOR_UI_ACTION_ERROR_'+type(exc).__name__+'_AT_'+stage
    finally:
        scope.close()

def quarantine_download(tmp, item, backup, reason, actual_name):
    digest=sha256_file(tmp)
    folder=backup / 'Direct_GPT_UI_unverified_quarantine' / safe_part(item['gpt_id'],90) / safe_part(item['file_id'],90)
    folder.mkdir(parents=True,exist_ok=True)
    dest=folder / (digest+'.bin')
    if not dest.exists():
        with dest.open('xb') as out, tmp.open('rb') as src:shutil.copyfileobj(src,out)
    info={'source':'visible owner GPT editor; not promoted to Knowledge',
          'gpt_id':item['gpt_id'],'file_id_from_snapshot':item['file_id'],
          'name_expected':item['name'],'name_browser_suggested':actual_name,
          'size_expected':item['expected'],'size_actual':tmp.stat().st_size,
          'sha256':digest,'reason':reason}
    (folder / (digest+'.json')).write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    return dest


def store_exact(original_download,item,backup):
    """Verify source filename and size; never overwrite an existing original."""
    suggested=Path(original_download.suggested_filename).name
    with tempfile.TemporaryDirectory(prefix='direct-gpt-original-',dir=str(backup)) as folder:
        tmp=Path(folder)/'download.tmp'
        original_download.save_as(str(tmp))
        failed=original_download.failure()
        if failed:return 'DOWNLOAD_FAILED',f'Chrome: {failed}'
        actual=tmp.stat().st_size
        if norm(suggested)!=norm(item['name']):
            q=quarantine_download(tmp,item,backup,'SUGGESTED_NAME_MISMATCH',suggested)
            return 'NAME_MISMATCH',f'{suggested}; сохранено отдельно: {q}'
        if actual!=item['expected']:
            q=quarantine_download(tmp,item,backup,'WRONG_SIZE',suggested)
            return 'WRONG_SIZE',f'{actual} вместо {item["expected"]}; карантин: {q}'
        dst=item['dest']
        digest=sha256_file(tmp)
        if dst.exists():
            if dst.stat().st_size==actual and sha256_file(dst)==digest:
                return 'EXISTS_EXACT_HASH',str(dst)
            return 'EXISTING_FILE_CONFLICT','Уже есть другой файл: '+str(dst)
        dst.parent.mkdir(parents=True,exist_ok=True)
        try:
            with dst.open('xb') as out, tmp.open('rb') as src:
                shutil.copyfileobj(src,out)
        except FileExistsError:
            return 'EXISTING_FILE_CONFLICT','Кто-то успел создать файл: '+str(dst)
        if dst.stat().st_size!=actual or sha256_file(dst)!=digest:
            dst.unlink(missing_ok=True)
            return 'COPY_VERIFICATION_FAILED',str(dst)
        return 'SAVED_DIRECT_GPT_EDITOR_SIZE_MATCH',str(dst)+' | SHA-256 '+digest


def append_receipt(backup,item,status,note):
    path=backup/'direct_gpt_ui_knowledge_inventory.csv'
    path.parent.mkdir(parents=True,exist_ok=True)
    columns=['timestamp_utc','gpt_name','gpt_id','file_id','file_name','expected_bytes','status','note']
    empty=not path.exists() or path.stat().st_size==0
    with path.open('a',newline='',encoding='utf-8-sig' if empty else 'utf-8') as f:
        wr=csv.DictWriter(f,fieldnames=columns,delimiter=';')
        if empty:wr.writeheader()
        wr.writerow(dict(timestamp_utc=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                         gpt_name=item['gpt_name'],gpt_id=item['gpt_id'],file_id=item['file_id'],
                         file_name=item['name'],expected_bytes=item['expected'],status=status,note=note))
    return path





# Do not equate a preview/UI failure with HTTP 404. Only the user can mark a
# manually observed {"detail":"Not found."} as MANUAL_EDITOR_404.
DEFER_STATUSES = {'MANUAL_EDITOR_404', 'REVIEW_REQUIRED_UI_FAILURE',
                  'REVIEW_REQUIRED_DOWNLOAD_MISMATCH'}


def deferred_key(item):
    """Use the exact GPT and original file ID, never filename alone."""
    if not item['file_id']:
        raise ValueError('No original file_id in snapshot for '+item['name'])
    return item['gpt_id'] + '::' + item['file_id']


def deferred_registry_path(backup):
    return backup/'direct_gpt_deferred_originals.json'


def load_deferred(backup):
    path=deferred_registry_path(backup)
    if not path.exists():return {}
    data=json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data,dict) or data.get('schema')!=1 or not isinstance(data.get('items'),list):
        raise ValueError('Некорректный реестр отложенных оригиналов: '+str(path))
    result={}
    for x in data['items']:
        if not isinstance(x,dict) or x.get('status') not in DEFER_STATUSES:
            raise ValueError('Некорректная запись в реестре отложенных оригиналов')
        key=(x.get('gpt_id') or '')+'::'+(x.get('file_id') or '')
        if key in result:raise ValueError('Повторяющийся file_id в реестре: '+key)
        result[key]=x
    return result


def save_deferred(backup, values):
    path=deferred_registry_path(backup)
    path.parent.mkdir(parents=True,exist_ok=True)
    data={'schema':1,'description':'Unverified originals deferred for later direct or Library review; NOT restored',
          'items':list(values.values())}
    fd,tmp=tempfile.mkstemp(dir=str(path.parent),prefix='.direct-deferred-',suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
            f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return path


def set_deferred(backup, values, item, status, reason):
    if status not in DEFER_STATUSES:raise ValueError('Unknown defer status')
    key=deferred_key(item)
    values[key]={
        'gpt_id':item['gpt_id'],'gpt_name':item['gpt_name'],
        'file_id':item['file_id'],'filename':item['name'],
        'expected_bytes':item['expected'],'status':status,
        'reason':str(reason)[:400],'timestamp_utc':datetime.now(timezone.utc).isoformat(timespec='seconds')}
    return save_deferred(backup,values)


def reset_editor_after_failure(context, page, access, q):
    """Create a clean editor tab after a failed preview; never touch unrelated tabs."""
    children=[p for p in access.pages if p is not page] if access else []
    if access:access.close()
    for child in children:
        if not child.is_closed():
            try:child.close()
            except Exception:pass
    if page is not None and not page.is_closed():
        try:page.close()
        except Exception:pass
    new_page=context.new_page()
    new_access=AccessMonitor(new_page)
    try:
        new_page.goto(EDITOR.format(q['id']),wait_until='domcontentloaded',timeout=60000)
        new_page.wait_for_timeout(1300)
        if new_access.blocked():return new_page,new_access,'HTTP_OR_SECURITY_ACCESS_RESTRICTION'
        config_result=open_config_if_needed(new_page,[t['name'] for t in q['all_targets']])
        if config_result not in ('CONFIG_KNOWLEDGE_VISIBLE','CONFIG_FILENAMES_VISIBLE'):
            return new_page,new_access,config_result
        if new_access.blocked():return new_page,new_access,'HTTP_OR_SECURITY_ACCESS_RESTRICTION'
        return new_page,new_access,None
    except Exception as exc:
        return new_page,new_access,'RESET_EDITOR_FAILED_'+type(exc).__name__

def all_gpt_ids(blob):
    records=blob.get('gpts', []) if isinstance(blob,dict) else blob
    if not isinstance(records,list):raise ValueError('Snapshot: список GPT не найден')
    out=[]
    for record in records:
        if not isinstance(record,dict):continue
        r=record.get('resource',record)
        g=r.get('gizmo',r)
        ident=g.get('id')
        if isinstance(ident,str) and ident.startswith('g-'):
            out.append(ident)
    if len(set(out))!=len(out):raise ValueError('Snapshot содержит дубли GPT ID')
    return out


def current_verified(t):
    """Matching size only: audit heuristic, not independent origin proof."""
    return t['dest'].is_file() and t['dest'].stat().st_size==t['expected']


def choose_ids(args, all_ids):
    if args.all_gpts and (args.ids or args.gpt_id):
        raise ValueError('Выбери --all-gpts ИЛИ --ids/--gpt-id')
    if args.ids and args.gpt_id:
        raise ValueError('Выбери один формат: --ids или --gpt-id')
    if args.all_gpts:
        selected=list(all_ids)
        if args.start_at:
            if args.start_at not in selected:
                raise ValueError('GPT ID для --start-at отсутствует в snapshot')
            selected=selected[selected.index(args.start_at):]
    elif args.ids:
        selected=list(args.ids)
    else:
        selected=[args.gpt_id or DEFAULT_GPT]
    if len(set(selected))!=len(selected):raise ValueError('Повторяющийся GPT ID в параметрах')
    unknown=set(selected)-set(all_ids)
    if unknown:raise ValueError('GPT ID отсутствуют в snapshot: '+', '.join(sorted(unknown)))
    if args.start_at and not args.all_gpts:
        raise ValueError('--start-at используется только с --all-gpts')
    return selected



# ==== V8: shared-source provenance, independent from filename-only matching ====
# IMPORTANT: a donor is proof of its OWN GPT copy, not byte identity of another GPT.
from collections import defaultdict

DIRECT_SAVE_STATUS = 'SAVED_DIRECT_GPT_EDITOR_SIZE_MATCH'
MANUAL_SAVE_STATUSES = {'RESTORED_FROM_DIRECT_GPT_DOWNLOAD', 'ALREADY_PRESENT_EXACT_HASH'}


def _metadata_tuple(f):
    """Precisely the same matching fields as audit_shared_gpt_knowledge.py."""
    return (f.get('name') or '', f.get('size'), f.get('type') or '',
            f.get('created_at') or '', str(f.get('last_modified') or ''))


def build_shared_index(blob, backup):
    """Rebuild membership FROM snapshot, not possibly stale exported CSV files."""
    by_key = {}
    by_meta = defaultdict(list)
    all_ids = all_gpt_ids(blob)
    for ident in all_ids:
        name, targets = parse_gpt(None, backup, ident, blob=blob)
        record = next(r for r in blob['gpts']
                      if r.get('resource', r).get('gizmo', r).get('id') == ident)
        original_files = record.get('resource', record).get('files') or []
        if len(original_files) != len(targets):
            raise RuntimeError('Original snapshot Knowledge list changed unexpectedly: ' + ident)
        for target, source in zip(targets, original_files):
            k = deferred_key(target)
            if k in by_key:raise ValueError('Duplicate GPT/file_id in snapshot: ' + k)
            target['metadata_fingerprint'] = _metadata_tuple(source)
            by_key[k] = target
            by_meta[target['metadata_fingerprint']].append(target)
    shared = {}
    for metadata, members in by_meta.items():
        if len({x['gpt_id'] for x in members}) < 2:continue
        group_id = 'SHARED-' + hashlib.sha256(json.dumps(metadata, ensure_ascii=False,
                                         separators=(',',':')).encode('utf-8')).hexdigest()[:16]
        shared[group_id] = members
        for m in members:m['shared_group_id'] = group_id
    return by_key, shared


def trusted_direct_donors(backup, by_key):
    """Trust ONLY direct-editor or manual-direct receipts with a matching SHA-256.

    Size-only saved files, Library copies, imported plugin files and other sources
    are NOT admitted, even when all snapshot metadata agrees.
    """
    receipts = {}
    journal = backup / 'direct_gpt_ui_knowledge_inventory.csv'
    if journal.is_file():
        with journal.open('r', encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f, delimiter=';'):
                if r.get('status') != DIRECT_SAVE_STATUS:continue
                key = (r.get('gpt_id') or '') + '::' + (r.get('file_id') or '')
                match = re.search(r'SHA-256\s+([a-fA-F0-9]{64})(?:\b|$)',r.get('note') or '')
                if key not in by_key or not match:continue
                if r.get('file_name') != by_key[key]['name']:continue
                receipts[key] = (match.group(1).lower(),'DIRECT_EDITOR_RECEIPT_HASH_VERIFIED')
    manual_log = backup / 'direct_gpt_knowledge_import_receipts.jsonl'
    if manual_log.is_file():
        with manual_log.open('r', encoding='utf-8-sig') as f:
            for lineno,line in enumerate(f,1):
                if not line.strip():continue
                try:r=json.loads(line)
                except json.JSONDecodeError:continue
                if r.get('result') not in MANUAL_SAVE_STATUSES:continue
                if r.get('source_claim') != 'user_manual_download_from_GPT_Knowledge':continue
                key = (r.get('gpt_id') or '') + '::' + (r.get('file_id') or '')
                digest = (r.get('original') or {}).get('sha256')
                if key not in by_key or not isinstance(digest,str) or not re.fullmatch(r'[a-fA-F0-9]{64}',digest):continue
                if r.get('original_file_name') != by_key[key]['name']:continue
                receipts[key] = (digest.lower(),'MANUAL_DIRECT_GPT_RECEIPT_HASH_VERIFIED')
    trusted = {}
    for key,(expected_digest,source) in receipts.items():
        t=by_key[key]
        if not current_verified(t):continue
        if sha256_file(t['dest']) != expected_digest:continue
        trusted[key] = dict(t, sha256=expected_digest, provenance=source)
    return trusted


def shared_score(target, shared, trusted):
    gid=target.get('shared_group_id')
    if not gid:return 1
    members=shared[gid]
    trusted_in_group=any(deferred_key(m) in trusted for m in members)
    if trusted_in_group:return 0  # already has at least one known donor
    return sum(not current_verified(x) for x in members)  # potential coverage


def _group_valid_donors(group, trusted):
    donors=[trusted[deferred_key(t)] for t in group if deferred_key(t) in trusted]
    unique_hashes={d['sha256'] for d in donors}
    if len(unique_hashes)>1:return [],True
    return sorted(donors,key=lambda t:(t['gpt_id'],t['file_id'])),False


def shared_statistics(shared, trusted, backup):
    candidate_targets=[];conflicts=[];covered=0
    for gid,members in shared.items():
        donors,conflict=_group_valid_donors(members,trusted)
        if conflict:
            conflicts.append(gid)
            continue
        if donors:covered+=1
        for target in members:
            if deferred_key(target) in trusted or not donors:continue
            if not any(d['gpt_id']!=target['gpt_id'] for d in donors):continue
            candidate_targets.append(target)
    return {'groups':len(shared),'members':sum(len(g) for g in shared.values()),
            'trusted_donors':len([x for x in trusted.values() if x.get('shared_group_id')]),
            'groups_with_donor':covered,'candidate_targets':candidate_targets,
            'conflict_groups':conflicts}


def stage_shared_candidates(backup, shared, trusted):
    """Copy a donor into a SEPARATE candidate vault, NEVER to the original GPT path.

    Multiple donor SHA-256 values in one metadata group => conflict; do nothing.
    Do not overwrite or delete another copy. Verify source hash before staging.
    """
    staged=[];skipped=0;conflicts=[]
    for gid,members in shared.items():
        donors, conflict = _group_valid_donors(members,trusted)
        if conflict:
            conflicts.append(gid)
            continue
        if not donors:continue
        for target in members:
            if deferred_key(target) in trusted:continue
            donor=next((d for d in donors if d['gpt_id']!=target['gpt_id']),None)
            if donor is None:continue
            # Always validate bytes against receipt again before copy, in case
            # a user edited a donor file while the script was running.
            src=donor['dest'];expected_digest=donor['sha256']
            if not current_verified(donor) or sha256_file(src)!=expected_digest:
                raise RuntimeError('Trusted donor file changed during staging: '+str(src))
            folder=(backup/'Shared_Knowledge_candidates'/safe_part(target['gpt_id'],90)
                    /safe_part(target['file_id'],90))
            target_bin=folder/(expected_digest+'.bin')
            target_json=folder/(expected_digest+'.json')
            if target_bin.exists():
                if (target_bin.stat().st_size != target['expected']
                        or sha256_file(target_bin)!=expected_digest):
                    raise RuntimeError('Candidate vault hash/size conflict. Not overwritten: '+str(target_bin))
                skipped+=1
            else:
                folder.mkdir(parents=True,exist_ok=True)
                created=False
                try:
                    with target_bin.open('xb') as out,src.open('rb') as ins:
                        created=True
                        shutil.copyfileobj(ins,out,1024*1024)
                    if (target_bin.stat().st_size!=target['expected']
                            or sha256_file(target_bin)!=expected_digest):
                        raise RuntimeError('Candidate staging verification failed: '+str(target_bin))
                except Exception:
                    if created:target_bin.unlink(missing_ok=True)
                    raise
                staged.append((target,donor,target_bin))
            evidence={
                'schema':1,'status':'SHARED_SOURCE_CANDIDATE_NOT_TARGET_ORIGINAL',
                'warning':'Same metadata is NOT independent proof of identical bytes across GPTs or file IDs.',
                'created_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'group_id':gid,'matched_metadata':{
                    'filename':target['metadata_fingerprint'][0],
                    'size':target['metadata_fingerprint'][1],
                    'mime_type':target['metadata_fingerprint'][2],
                    'created_at':target['metadata_fingerprint'][3],
                    'last_modified':target['metadata_fingerprint'][4]},
                'target':{'gpt_id':target['gpt_id'],'gpt_name':target['gpt_name'],
                          'file_id':target['file_id'],'original_knowledge_path':str(target['dest']),
                          'original_verified':False},
                'donor':{'gpt_id':donor['gpt_id'],'gpt_name':donor['gpt_name'],
                         'file_id':donor['file_id'],'original_knowledge_path':str(src),
                         'verified_donor_sha256':expected_digest,
                         'donor_provenance':donor['provenance']},
                'candidate_path':str(target_bin),
                'candidate_sha256':expected_digest,
            }
            if not target_json.exists():
                try:
                    with target_json.open('x',encoding='utf-8') as f:
                        json.dump(evidence,f,ensure_ascii=False,indent=2)
                except FileExistsError:pass
    return staged,skipped,conflicts


def shared_report(backup, shared, trusted):
    """Offline report includes verified donor/candidate provenance; no data copying."""
    path=backup/'shared_knowledge_v8_inventory.csv'
    cols=['group_id','target_gpt','target_gpt_id','target_file_id','filename',
          'expected_bytes','direct_original_present_by_size','trusted_direct_source',
          'donor_gpt','donor_gpt_id','donor_file_id','donor_sha256',
          'candidate_exists_verified','candidate_vs_unverified_local','status']
    rows=[]
    for gid,members in sorted(shared.items(),key=lambda kv:-len(kv[1])):
        donors, conflict=_group_valid_donors(members,trusted)
        for t in members:
            donor=next((d for d in donors if d['gpt_id']!=t['gpt_id']),None)
            candidate_file=(backup/'Shared_Knowledge_candidates'/safe_part(t['gpt_id'],90)
                  /safe_part(t['file_id'],90)/((donor['sha256'] if donor else '')+'.bin')) if donor else None
            candidate_ok=bool(candidate_file and candidate_file.is_file()
                    and candidate_file.stat().st_size==t['expected']
                    and sha256_file(candidate_file)==donor['sha256'])
            is_present=current_verified(t)
            direct=deferred_key(t) in trusted
            comparison=''
            if candidate_ok and is_present and not direct:
                comparison=('MATCHES_UNVERIFIED_LOCAL_BYTES' if sha256_file(t['dest'])==donor['sha256']
                            else 'DIFFERS_FROM_UNVERIFIED_LOCAL_BYTES')
            status=('DONOR_HASH_CONFLICT' if conflict else
                    'OWN_GPT_DIRECT_SOURCE' if direct else
                    'LOCAL_SIZE_ONLY_SOURCE_NOT_VERIFIED' if is_present else
                    'SHARED_CANDIDATE_STAGED' if candidate_ok else
                    'DONOR_AVAILABLE_NOT_STAGED' if donor else 'NO_TRUSTED_DONOR')
            rows.append(dict(group_id=gid,target_gpt=t['gpt_name'],target_gpt_id=t['gpt_id'],
              target_file_id=t['file_id'],filename=t['name'],expected_bytes=t['expected'],
              direct_original_present_by_size='YES' if is_present else 'NO',
              trusted_direct_source='YES' if direct else 'NO',
              donor_gpt=donor['gpt_name'] if donor else '',
              donor_gpt_id=donor['gpt_id'] if donor else '',
              donor_file_id=donor['file_id'] if donor else '',
              donor_sha256=donor['sha256'] if donor else '',
              candidate_exists_verified='YES' if candidate_ok else 'NO',
              candidate_vs_unverified_local=comparison,status=status))
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=cols,delimiter=';')
        writer.writeheader();writer.writerows(rows)
    return path

def hold_windows_awake(enable):
    """Keep the Windows machine running while unattended; display may sleep normally.

    This is process-scoped; it does NOT change persistent Windows power settings.
    """
    if not enable or os.name!='nt':
        return False
    try:
        import ctypes
        # ES_CONTINUOUS | ES_SYSTEM_REQUIRED; monitor may sleep.
        result=ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        return bool(result)
    except Exception:
        return False


def release_windows_awake():
    if os.name!='nt':return
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass


def append_gpt_issue(backup, q, status, detail):
    """Append minimal, provenance-safe GPT-level incident; never write page HTML or URLs."""
    path=backup/'direct_gpt_ui_gpt_issues_v11.jsonl'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a',encoding='utf-8') as out:
        out.write(json.dumps({
            'timestamp_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'gpt_id':q['id'],'gpt_name':q['name'],
            'pending_files_count':len(q['pending']),
            'status':str(status)[:100], 'detail':str(detail)[:250]},
            ensure_ascii=False)+'\n')
    return path


def main(argv=None):
    ap=argparse.ArgumentParser(description='Скачать оригинальный Knowledge через видимые редакторы выбранных Custom GPT')
    ap.add_argument('--snapshot',default='Custom_GPT_backup/gpt_list_snapshot.json')
    ap.add_argument('--backup',default='Custom_GPT_backup')
    target=ap.add_argument_group('Выбор GPT')
    target.add_argument('--all-gpts',action='store_true',help='Обойти GPT из snapshot; фактические загрузки ограничены лимитами')
    target.add_argument('--ids',nargs='+',help='Один или несколько GPT ID, в заданном порядке')
    target.add_argument('--gpt-id',help='Один GPT ID; иначе используйте --all-gpts или --ids')
    target.add_argument('--start-at',help='С какого GPT ID начать, только с --all-gpts')
    target.add_argument('--skip-gpt-name-regex',default='',help='Временно исключить GPT по регулярному выражению из ИМЕНИ. Только в текущем запуске, snapshot и отложенный реестр не меняются')
    target.add_argument('--skip-gpt-ids',nargs='*',default=[],help='Временно исключить заданные GPT ID; не изменяет snapshot')
    target.add_argument('--file',help='Имя Knowledge-файла; только с одним выбранным GPT')
    ap.add_argument('--profile',default='.chatgpt_backup_normal_chrome_profile')
    ap.add_argument('--cdp-port',type=int)
    ap.add_argument('--attach',action='store_true')
    ap.add_argument('--plan-only',action='store_true',help='Только сверить очередь, без Chrome и сети')
    ap.add_argument('--inspect-editor',action='store_true',help='Только осмотреть раздел Знания одного GPT')
    ap.add_argument('--inspect-target',action='store_true',help='Диагностика строки одного файла без скачивания')
    ap.add_argument('--confirm-access-restored',action='store_true')
    ap.add_argument('--max-attempts',type=int,default=3,help='Общий лимит попыток по ВСЕМ GPT (1–3000)')
    ap.add_argument('--max-files',type=int,default=3,help='Общий лимит успешно сохранённых оригиналов (1–3000)')
    ap.add_argument('--max-gpts',type=int,default=2,help='Максимум посещённых редакторов за запуск (1–250)')
    ap.add_argument('--max-attempts-per-gpt',type=int,default=20,help='Максимум попыток в каждом GPT (1–50)')
    ap.add_argument('--record-manual-404',action='store_true',
                    help='БЕЗ СЕТИ: отметить ровно один файл как HTTP 404, подтверждённый вручную в редакторе')
    ap.add_argument('--retry-deferred',action='store_true',
                    help='Явно повторить ВСЕ отложенные файлы, включая вручную подтверждённые 404')
    ap.add_argument('--retry-ui-errors',action='store_true',
                    help='Повторить ТОЛЬКО UI-ошибки, НЕ затрагивая MANUAL_EDITOR_404')
    ap.add_argument('--proven-first',action='store_true',
                    help='Сначала редакторы GPT, где уже есть доказанные прямые успешные загрузки')
    ap.add_argument('--continue-on-file-error',action='store_true',
                    help='Отложить отдельную ошибку скачивания и продолжить с чисто открытым редактором')
    ap.add_argument('--max-ui-errors-per-gpt',type=int,default=20,
                    help='После N UI-ошибок в одном GPT перейти к следующему редактору (1–50)')
    ap.add_argument('--max-consecutive-errors',type=int,default=3,
                    help='Для обычного запуска: остановка после N последовательных ошибок файлов (1–30). В --unattended UI-ошибки ограничиваются на уровне GPT')
    ap.add_argument('--shared-priority',action='store_true',
                    help='Пробовать сначала GPT/файлы с максимальным покрытием общих баз')
    ap.add_argument('--shared-report',action='store_true',
                    help='БЕЗ СЕТИ: создать CSV с подтверждёнными донорами и статусами кандидатов')
    ap.add_argument('--stage-shared-candidates',action='store_true',
                    help='БЕЗ СЕТИ: скопировать подтверждённых доноров в ОТДЕЛЬНЫЙ каталог кандидатов')
    ap.add_argument('--no-auto-stage-shared',action='store_true',
                    help='Не создавать кандидатов при новых удачных прямых загрузках')
    ap.add_argument('--allow-sleep',action='store_true',help='Не препятствовать переходу Windows в сон при --unattended')
    ap.add_argument('--unattended',action='store_true',
                    help='Длительный обход: до 250 GPT и 3000 попыток; продолжать после UI-ошибок, но СТОП при 401/403/429, блокировке, системном сбое')
    ap.add_argument('--max-failed-gpts-in-a-row',type=int,default=25,
                    help='Предохранитель при массовой поломке UI: остановка после N GPT без единого успешного скачивания (1–250), только для --unattended')
    ap.add_argument('--pause-min',type=float,default=15)
    ap.add_argument('--pause-max',type=float,default=30)
    args=ap.parse_args(argv)
    if args.unattended:
        # Explicit long-run mode only. Normal short-run defaults remain unchanged.
        args.all_gpts=True
        args.continue_on_file_error=True
        args.proven_first=True
        args.shared_priority=True
        args.max_gpts=250
        args.max_attempts=3000
        args.max_files=3000
        args.max_attempts_per_gpt=50
        args.max_ui_errors_per_gpt=3
    if not 1<=args.max_attempts<=3000:ap.error('--max-attempts: от 1 до 3000')
    if not 1<=args.max_files<=3000:ap.error('--max-files: от 1 до 3000')
    if not 1<=args.max_gpts<=250:ap.error('--max-gpts: от 1 до 250')
    if not 1<=args.max_attempts_per_gpt<=50:ap.error('--max-attempts-per-gpt: от 1 до 50')
    if not 0<=args.pause_min<=args.pause_max<=3600:ap.error('Неверный интервал паузы')
    if not 1<=args.max_consecutive_errors<=30:ap.error('--max-consecutive-errors: от 1 до 30')
    if not 1<=args.max_ui_errors_per_gpt<=50:ap.error('--max-ui-errors-per-gpt: от 1 до 50')
    if not 1<=args.max_failed_gpts_in_a_row<=250:ap.error('--max-failed-gpts-in-a-row: от 1 до 250')
    snapshot=Path(args.snapshot).expanduser().resolve()
    backup=Path(args.backup).expanduser().resolve()
    if not snapshot.is_file():ap.error(f'Нет snapshot: {snapshot}')
    blob=json.loads(snapshot.read_text(encoding='utf-8-sig'))
    by_key, shared=build_shared_index(blob,backup)
    trusted=trusted_direct_donors(backup,by_key)
    stats=shared_statistics(shared,trusted,backup)
    if args.shared_report and args.stage_shared_candidates:
        ap.error('Выбери одно: --shared-report или --stage-shared-candidates')
    if args.retry_deferred and args.retry_ui_errors:ap.error('Выбери --retry-deferred ИЛИ --retry-ui-errors')
    if args.plan_only and args.stage_shared_candidates:
        ap.error('--plan-only ничего не записывает; для копирования кандидатов используй только --stage-shared-candidates')
    try:
        ids=choose_ids(args,all_gpt_ids(blob))
    except ValueError as exc:ap.error(str(exc))
    # Exclusions apply ONLY to this run; never remove or mark source records
    # recovered/deferred merely because this GPT is not being visited.
    skip_ids=set(args.skip_gpt_ids)
    unknown_skip=skip_ids-set(all_gpt_ids(blob))
    if unknown_skip:ap.error('Нет таких GPT ID для --skip-gpt-ids: '+', '.join(sorted(unknown_skip)))
    try:
        skip_regex=re.compile(args.skip_gpt_name_regex,re.I) if args.skip_gpt_name_regex else None
    except re.error as exc:
        ap.error('Неверное --skip-gpt-name-regex: '+str(exc))
    before_filter=len(ids)
    skipped_ids=[]
    if skip_ids or skip_regex:
        kept=[]
        for ident in ids:
            gname,_=parse_gpt(snapshot,backup,ident,blob=blob)
            if ident in skip_ids or (skip_regex and skip_regex.search(gname)):
                skipped_ids.append((ident,gname))
            else:
                kept.append(ident)
        ids=kept
    if not ids:
        print('После временного исключения GPT пустая очередь. Snapshot и файлы не изменены.')
        return 0
    if (args.file or args.inspect_editor or args.inspect_target) and len(ids)!=1:
        ap.error('--file, --inspect-editor и --inspect-target требуют ровно одного GPT')
    if args.inspect_target and not args.file:
        ap.error('--inspect-target требует --file')
    if args.record_manual_404 and (not args.file or len(ids)!=1):
        ap.error('--record-manual-404 требует --file и один --ids/--gpt-id')
    deferred=load_deferred(backup)
    jobs=[]
    gpt_plans=[]
    duplicates=0
    for ident in ids:
        name,all_targets=parse_gpt(snapshot,backup,ident,blob=blob)
        # Reattach shared metadata to the run's targets: they are fresh objects
        # from parse_gpt, distinct from build_shared_index's target instances.
        for t in all_targets:
            original=by_key[deferred_key(t)]
            t['shared_group_id']=original.get('shared_group_id')
            t['metadata_fingerprint']=original['metadata_fingerprint']
        counts={norm(x['name']):sum(norm(f['name'])==norm(x['name']) for f in all_targets) for x in all_targets}
        if args.file:
            selected=[t for t in all_targets if norm(t['name'])==norm(args.file)]
            if len(selected)!=1:ap.error('Не найдено ровно одного файла '+args.file+' в GPT '+name)
        else:
            selected=list(all_targets)
        verified=sum(current_verified(t) for t in all_targets)
        all_missing=[t for t in selected if not current_verified(t)]
        pending=[t for t in all_missing if (args.retry_deferred or deferred_key(t) not in deferred or
                (args.retry_ui_errors and deferred.get(deferred_key(t),{}).get('status')=='REVIEW_REQUIRED_UI_FAILURE'))]
        already_deferred=[t for t in all_missing if t not in pending and deferred_key(t) in deferred]
        ambiguous=[t for t in pending if counts[norm(t['name'])]!=1]
        duplicates+=len(ambiguous)
        gpt_plans.append({'id':ident,'name':name,'all_targets':all_targets,
                          'pending':pending,'deferred':already_deferred,
                          'ambiguous':ambiguous,'verified':verified})
        jobs.extend(pending)
    if args.shared_priority:
        for q in gpt_plans:
            q['pending'].sort(key=lambda t:(-shared_score(t,shared,trusted),t['ordinal']))
        gpt_plans.sort(key=lambda q:(-max([shared_score(t,shared,trusted)
                                               for t in q['pending']] or [0]),
                                        -sum(shared_score(t,shared,trusted)>1 for t in q['pending'])))
        jobs=[t for q in gpt_plans for t in q['pending']]
    if args.proven_first:
        # A successful real download is better evidence of a usable UI source than
        # a metadata sharing score. Existing Library-only copies give zero credit.
        proven_counts={}
        for donor in trusted.values():
            proven_counts[donor['gpt_id']]=proven_counts.get(donor['gpt_id'],0)+1
        for q in gpt_plans:
            if args.shared_priority:
                q['pending'].sort(key=lambda t:(-shared_score(t,shared,trusted),t['ordinal']))
        gpt_plans.sort(key=lambda q:(-proven_counts.get(q['id'],0),
            -max([shared_score(t,shared,trusted) for t in q['pending']] or [0])))
        jobs=[t for q in gpt_plans for t in q['pending']]
    if args.unattended:
        # Prior direct-success sources, then not-yet-tried sources, then failed-UI sources.
        # Crucially, previously failing GPTs are not removed from this run.
        previous_ui_errors={}
        for d in deferred.values():
            if d['status'] in ('REVIEW_REQUIRED_UI_FAILURE','REVIEW_REQUIRED_DOWNLOAD_MISMATCH'):
                ident=d['gpt_id']
                previous_ui_errors[ident]=previous_ui_errors.get(ident,0)+1
        prior_success={d['gpt_id'] for d in trusted.values()}
        gpt_plans.sort(key=lambda q:(
            0 if q['id'] in prior_success else 2 if previous_ui_errors.get(q['id'],0) else 1,
            -max([shared_score(t,shared,trusted) for t in q['pending']] or [0]),
            -len(q['pending'])))
        jobs=[t for q in gpt_plans for t in q['pending']]
    if args.record_manual_404:
        target=gpt_plans[0]['all_targets']
        selected=[t for t in target if norm(t['name'])==norm(args.file)]
        if len(selected)!=1:ap.error('Для ручной отметки нужен ровно один file_id')
        t=selected[0]
        if current_verified(t):ap.error('Этот файл уже присутствует на диске; отметка 404 не нужна')
        path=set_deferred(backup,deferred,t,'MANUAL_EDITOR_404',
             'Владелец вручную проверил скачивание из GPT Editor; ответ: {"detail":"Not found."}')
        append_receipt(backup,t,'MANUAL_EDITOR_404',
             'Подтверждено владельцем в редакторе; оригинал не сохранён; второй этап: Library')
        print('БЕЗ СЕТИ. Отложен оригинал:',t['gpt_name'],'/',t['name'])
        print('GPT ID:',t['gpt_id'],'| file ID:',t['file_id'])
        print('Причина: ручная проверка в редакторе вернула HTTP 404 / Not found.')
        print('Для следующего запуска будет пропущен. Реестр:',path)
        return 0
    if skipped_ids:
        print(f'ВРЕМЕННО ПРОПУЩЕНО GPT: {len(skipped_ids)} из {before_filter} выбранных. Snapshot/реестры не менялись.')
        print('  Примеры: '+', '.join(name for _,name in skipped_ids[:8]))
    print(f'Общие базы: {stats["groups"]} групп / {stats["members"]} записей; '
          f'достоверных прямых доноров: {stats["trusted_donors"]}; '
          f'групп с донором: {stats["groups_with_donor"]}; '
          f'целей без прямого подтверждения с донором: {len(stats["candidate_targets"])}; '
          f'конфликтных SHA-256 групп: {len(stats["conflict_groups"])}.')
    if args.stage_shared_candidates:
        staged,already,conflicts=stage_shared_candidates(backup,shared,trusted)
        p=shared_report(backup,shared,trusted)
        print('БЕЗ СЕТИ. Кандидатов скопировано в ОТДЕЛЬНЫЙ каталог:',len(staged),
              '; уже существовало:',already,'; конфликтных групп:',len(conflicts))
        for t,d,candidate_path in staged[:25]:
            print('  КАНДИДАТ:',t['gpt_name'],'/',t['name'],'←',d['gpt_name'],
                  '|',candidate_path)
        print('Целевые оригиналы НЕ заполнялись. Отчёт:',p)
        return 0
    if args.shared_report:
        p=shared_report(backup,shared,trusted)
        print('БЕЗ СЕТИ, только отчёт:',p)
        return 0
    total=sum(len(q['all_targets']) for q in gpt_plans)
    confirmed=sum(q['verified'] for q in gpt_plans)
    print(f'GPT в выбранном списке: {len(gpt_plans)}; Knowledge: {total}; на диске совпадает размер: {confirmed}; не хватает: {total-confirmed}.')
    num_deferred=sum(len(q['deferred']) for q in gpt_plans)
    print(f'Ожидают действий: {len(jobs)}; отложено для второго этапа: {num_deferred}; с неоднозначным именем внутри GPT: {duplicates}.')
    if num_deferred and not args.retry_deferred:
        print('Реестр отложенных оригиналов:',deferred_registry_path(backup))
    print(f'Лимиты: редакторов {args.max_gpts}, попыток {args.max_attempts}, новых файлов {args.max_files}, на GPT {args.max_attempts_per_gpt}; UI-ошибок на GPT: {args.max_ui_errors_per_gpt}.')
    if args.unattended:
        print('РЕЖИМ ДЛИТЕЛЬНОГО ОБХОДА: ошибки файлов не прерывают общую очередь; после 3 ошибок в GPT — следующий GPT.')
        print('ПРЕДОХРАНИТЕЛИ: HTTP 401/403/429, предупреждение об ограничении доступа и', args.max_failed_gpts_in_a_row, 'GPT подряд без успеха.')
        print('Достоверность: копии из Library только по размеру НЕ объявляются прямыми оригиналами.')
    if args.plan_only:
        print('ОФЛАЙН-РЕЖИМ: Chrome и сеть не используются.')
        if args.shared_priority:print('Приоритет: сначала общий источник с максимальным числом недостающих копий.')
        if args.proven_first:print('Приоритет выше общего: GPT с уже подтверждёнными прямыми загрузками.')
        for q in gpt_plans:
            if q['pending']:
                print(f'  {q["name"]} [{q["id"]}]: {q["verified"]}/{len(q["all_targets"])}; ожидают {len(q["pending"])}')
                if len(gpt_plans)==1:
                    for t in q['pending'][:20]:
                        print(f'    → {t["name"]} | {t["expected"]} байт | file_id {t["file_id"]}')
        return 0
    if not args.attach:ap.error('Для сети нужен --attach к уже открытому Chrome')
    if not args.confirm_access_restored:ap.error('Вначале проверь доступ вручную и укажи --confirm-access-restored')
    backup.mkdir(parents=True,exist_ok=True)
    if not args.no_auto_stage_shared and not (args.inspect_editor or args.inspect_target):
        staged,already,conflicts=stage_shared_candidates(backup,shared,trusted)
        if staged:
            print('Проверенные доноры: создано отдельных кандидатов для других GPT:',len(staged))
    from playwright.sync_api import sync_playwright
    port=existing_chrome_port(Path(args.profile).expanduser().resolve(),args.cdp_port)
    playwright=None
    overall_attempts=0;overall_saved=0;opened=0
    stop='QUEUE_COMPLETE'
    consecutive_errors=0
    failed_gpts_in_a_row=0
    skipped_gpts_count=0
    wake_held=hold_windows_awake(args.unattended and not args.allow_sleep)
    if args.unattended and os.name=='nt':
        print('Режим без сна Windows:', 'активен (экран может выключаться)' if wake_held else 'не активирован')
    try:
        playwright=sync_playwright().start()
        browser=playwright.chromium.connect_over_cdp(f'http://127.0.0.1:{port}',timeout=20000)
        if not browser.contexts:raise RuntimeError('Нет авторизованного Chrome context')
        context=browser.contexts[0]
        for q in gpt_plans:
            if overall_attempts>=args.max_attempts:stop='ATTEMPT_LIMIT';break
            if overall_saved>=args.max_files:stop='FILE_LIMIT';break
            # A GPT with no missing Knowledge does not count against max-gpts.
            if not q['pending']:continue
            actionable=[t for t in q['pending'] if t not in q['ambiguous']]
            if not actionable:
                for t in q['ambiguous']:
                    append_receipt(backup,t,'AMBIGUOUS_NAME_WITHIN_GPT','Два файла с одинаковым именем в одном GPT')
                continue
            if opened>=args.max_gpts:stop='GPT_LIMIT';break
            paused_for_editor=False
            if overall_attempts:
                seconds=random.uniform(args.pause_min,args.pause_max)
                print(f'Пауза перед следующим редактором: {seconds:.1f} сек.')
                time.sleep(seconds)
                paused_for_editor=True
            per_gpt_attempts=0
            saved_at_gpt_start=overall_saved
            ui_errors_this_gpt=0
            page=None;access=None
            needs_reset=False
            try:
                page=context.new_page()
                opened+=1
                access=AccessMonitor(page)
                print(f'\n[{opened}/{args.max_gpts}] Редактор: {q["name"]} ({q["id"]}), осталось {len(q["pending"])} Knowledge')
                page.goto(EDITOR.format(q['id']),wait_until='domcontentloaded',timeout=60000)
                page.wait_for_timeout(1300)
                if access.blocked():
                    stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION';break
                config_result=open_config_if_needed(page,[t['name'] for t in q['all_targets']])
                print('  Конфигурация →',config_result)
                if config_result not in ('CONFIG_KNOWLEDGE_VISIBLE','CONFIG_FILENAMES_VISIBLE'):
                    p=editor_probe(page,backup/'direct_gpt_editor_ui_probe.json',config_result,
                                   expected_files=[t['name'] for t in q['all_targets']])
                    print('  Раздел Знания не открыт; диагностика:',p)
                    if access.blocked():
                        stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION';break
                    if args.unattended:
                        print('  → GPT пропущен на этом проходе. Его Knowledge НЕ объявлены отсутствующими навсегда.')
                        append_gpt_issue(backup,q,'CONFIG_NOT_VISIBLE',config_result)
                        skipped_gpts_count+=1
                        failed_gpts_in_a_row+=1
                        if failed_gpts_in_a_row>=args.max_failed_gpts_in_a_row:
                            stop='SYSTEMIC_EDITOR_FAILURE_LIMIT';break
                        continue
                    stop=config_result;break
                deadline=time.monotonic()+22
                while time.monotonic()<deadline and not any(evidence_filename_visible(page,t['name']) for t in q['all_targets']):
                    if access.blocked():break
                    page.wait_for_timeout(400)
                if args.inspect_editor or args.inspect_target:
                    target=q['pending'][0] if q['pending'] else q['all_targets'][0]
                    if args.inspect_target:
                        node,_=find_filename_node(page,target['name'])
                        if node:scroll_target_once(node)
                    p=editor_probe(page,backup/'direct_gpt_editor_ui_probe.json','INSPECT_TARGET' if args.inspect_target else 'INSPECT_EDITOR',
                                   target['name'] if args.inspect_target else '',[t['name'] for t in q['all_targets']])
                    print('  Диагностика:',p)
                    print('  Knowledge-файлов, видимых в редакторе:',
                          sum(bool(evidence_filename_visible(page,t['name'])) for t in q['all_targets']),
                          '/',len(q['all_targets']))
                    stop='INSPECT_ONLY';break
                if access.blocked():stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION';break
                for t in q['pending']:
                    if overall_attempts>=args.max_attempts:stop='ATTEMPT_LIMIT';break
                    if overall_saved>=args.max_files:stop='FILE_LIMIT';break
                    if per_gpt_attempts>=args.max_attempts_per_gpt:break
                    if t in q['ambiguous']:
                        append_receipt(backup,t,'AMBIGUOUS_NAME_WITHIN_GPT','Два файла с одинаковым именем в одном GPT')
                        print('  → Неоднозначное имя внутри GPT:',t['name'])
                        continue
                    if current_verified(t):continue
                    if page is not None and page.is_closed():needs_reset=True
                    if needs_reset:
                        print('  Сброс просмотрщика: заново открываю редактор перед следующим файлом.')
                        page,access,reset_error=reset_editor_after_failure(context,page,access,q)
                        needs_reset=False
                        if reset_error:
                            p=editor_probe(page,backup/'direct_gpt_editor_ui_probe.json',reset_error,
                                t['name'],[x['name'] for x in q['all_targets']])
                            print('  Повторное открытие редактора не удалось:',reset_error,'|',p)
                            if access.blocked():
                                stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION';break
                            if args.unattended:
                                append_gpt_issue(backup,q,'RESET_EDITOR_FAILED',reset_error)
                                skipped_gpts_count+=1
                                set_deferred(backup,deferred,t,'REVIEW_REQUIRED_UI_FAILURE',reset_error)
                                # Do not try remaining file buttons on an uninitialized editor.
                                break
                            stop=reset_error;break
                    if access.blocked():stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION';break
                    if overall_attempts and not (paused_for_editor and per_gpt_attempts==0):
                        seconds=random.uniform(args.pause_min,args.pause_max)
                        print(f'  Обычная пауза: {seconds:.1f} сек.')
                        time.sleep(seconds)
                        if access.blocked():stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION';break
                    overall_attempts+=1
                    per_gpt_attempts+=1
                    print(f'  [{overall_attempts}/{args.max_attempts}] Knowledge: {t["name"]}')
                    preview_path=unique_preview_probe_path(backup,t)
                    node,reason=find_filename_node(page,t['name'])
                    if reason:
                        p=editor_probe(page,backup/'direct_gpt_editor_ui_probe.json',reason,t['name'],[x['name'] for x in q['all_targets']])
                        append_receipt(backup,t,reason,'Диагностика '+str(p))
                        print('    →',reason,'|',p)
                        if args.continue_on_file_error and reason not in ('AMBIGUOUS_EXACT_KNOWLEDGE_BUTTON',):
                            set_deferred(backup,deferred,t,'REVIEW_REQUIRED_UI_FAILURE',reason)
                            consecutive_errors+=1
                            ui_errors_this_gpt+=1
                            if not args.unattended and consecutive_errors>=args.max_consecutive_errors:
                                stop='CONSECUTIVE_ERRORS_LIMIT';break
                            if ui_errors_this_gpt>=args.max_ui_errors_per_gpt:
                                print('  → Лимит UI-ошибок в этом GPT достигнут; перехожу к следующему GPT.')
                                break
                            needs_reset=True;continue
                        stop='EDITOR_UI_MISMATCH';break
                    downloaded,reason=try_file_download(page,node,t['name'],[x['name'] for x in q['all_targets']],
                        preview_probe=preview_path,access=access)
                    if access.blocked():
                        append_receipt(backup,t,'HTTP_ACCESS_RESTRICTION',str(access.status or 'security warning'))
                        print('  Остановка: HTTP / защитное ограничение:',access.status)
                        stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION';break
                    if not downloaded:
                        p=editor_probe(page,backup/'direct_gpt_editor_ui_probe.json',reason,t['name'],[x['name'] for x in q['all_targets']])
                        preview_note=_save_preview_after_run(preview_path,backup,t,reason)
                        append_receipt(backup,t,reason,'Диагностика '+str(p)+preview_note)
                        print('    →',reason,'|',p)
                        if args.continue_on_file_error:
                            set_deferred(backup,deferred,t,'REVIEW_REQUIRED_UI_FAILURE',reason)
                            print('    → UI-ошибка: отложено для повторной проверки, НЕ HTTP 404. Оригинал не получен.')
                            consecutive_errors+=1
                            ui_errors_this_gpt+=1
                            if not args.unattended and consecutive_errors>=args.max_consecutive_errors:
                                stop='CONSECUTIVE_ERRORS_LIMIT';break
                            if ui_errors_this_gpt>=args.max_ui_errors_per_gpt:
                                print('  → Лимит UI-ошибок в этом GPT достигнут; перехожу к следующему GPT.')
                                break
                            needs_reset=True;continue
                        stop='DOWNLOAD_OR_UI_ERROR';break
                    status,note=store_exact(downloaded,t,backup)
                    append_receipt(backup,t,status,note)
                    print('    →',status,'|',note)
                    if status=='SAVED_DIRECT_GPT_EDITOR_SIZE_MATCH':
                        if page.is_closed():needs_reset=True
                        overall_saved+=1
                        consecutive_errors=0
                        # New direct source is trusted only after the saved
                        # bytes were checked and hash recorded in our journal.
                        trusted=trusted_direct_donors(backup,by_key)
                        if not args.no_auto_stage_shared and t.get('shared_group_id'):
                            newly_staged,already,conflicts=stage_shared_candidates(backup,shared,trusted)
                            if newly_staged:
                                print('    → КАНДИДАТЫ общей базы (НЕ оригиналы другого GPT):',
                                      len(newly_staged))
                    elif status=='EXISTS_EXACT_HASH':
                        consecutive_errors=0
                    elif args.continue_on_file_error and status in ('WRONG_SIZE','NAME_MISMATCH','DOWNLOAD_FAILED'):
                        set_deferred(backup,deferred,t,'REVIEW_REQUIRED_DOWNLOAD_MISMATCH',status+': '+note)
                        print('    → Отложено для сверки; непроверенное содержимое не размещено в Knowledge.')
                        consecutive_errors+=1
                        ui_errors_this_gpt+=1
                        if not args.unattended and consecutive_errors>=args.max_consecutive_errors:
                            stop='CONSECUTIVE_ERRORS_LIMIT';break
                        if ui_errors_this_gpt>=args.max_ui_errors_per_gpt:
                            print('  → Лимит ошибок в этом GPT достигнут; перехожу к следующему GPT.')
                            break
                        needs_reset=True;continue
                    else:stop='FILE_VERIFICATION_ERROR';break
                if stop not in ('QUEUE_COMPLETE','ATTEMPT_LIMIT','FILE_LIMIT'):
                    break
                if stop in ('ATTEMPT_LIMIT','FILE_LIMIT'):break
            except Exception as exc:
                if access is not None and access.blocked():
                    stop='HTTP_OR_SECURITY_ACCESS_RESTRICTION'
                    print('  Остановка: ограничение доступа во время работы с GPT:',access.status)
                    break
                if not args.unattended:
                    raise
                # Browser/CDP disconnect is systemic and should stop immediately.
                if type(exc).__name__ in ('TargetClosedError','Error') and any(
                    marker in str(exc).lower() for marker in
                    ('browser has been closed','connection closed','browser context closed',
                     'target page, context or browser has been closed')):
                    stop='BROWSER_CONNECTION_LOST'
                    print('  Остановка: соединение с Chrome разорвано:',type(exc).__name__)
                    break
                print('  GPT-level UI/network error (not an HTTP 404):',type(exc).__name__)
                append_gpt_issue(backup,q,'GPT_UI_OR_NAVIGATION_ERROR',type(exc).__name__+': '+str(exc)[:180])
                skipped_gpts_count+=1
                # Move on to the next GPT; do not mark unattempted files as restored/deferred.
            finally:
                if access:access.close()
                if page and not page.is_closed():
                    try:page.close()
                    except Exception:pass
            if args.unattended:
                if overall_saved>saved_at_gpt_start:
                    failed_gpts_in_a_row=0
                else:
                    failed_gpts_in_a_row+=1
                    print('  → GPT без новых оригиналов; подряд:',failed_gpts_in_a_row)
                    if failed_gpts_in_a_row>=args.max_failed_gpts_in_a_row:
                        stop='SYSTEMIC_EDITOR_FAILURE_LIMIT'
                        print('  Безопасная остановка: слишком много GPT без единого нового файла.')
                        break
        if stop=='QUEUE_COMPLETE' and (deferred or skipped_gpts_count or duplicates):
            stop='QUEUE_EXHAUSTED_WITH_UNRESOLVED_FILES'
        print(f'\nИтого: открыто GPT {opened}; попыток {overall_attempts}; новых оригиналов {overall_saved}.')
        print('Причина остановки:',stop)
        print('Отложено на последующую проверку в этом реестре:',len(deferred))
        if args.unattended:print('GPT с ошибкой открытия/сброса редактора:',skipped_gpts_count)
        print('Реестр:',deferred_registry_path(backup))
        if not args.no_auto_stage_shared:
            print('Кандидаты общих баз:',backup/'Shared_Knowledge_candidates')
        print('Отчёт общих баз:',shared_report(backup,shared,trusted))
        print('Журнал:',backup/'direct_gpt_ui_knowledge_inventory.csv')
        if args.unattended:print('Журнал сбоев редакторов:',backup/'direct_gpt_ui_gpt_issues_v11.jsonl')
        return 0 if stop in ('QUEUE_COMPLETE','QUEUE_EXHAUSTED_WITH_DEFERRED_FILES','QUEUE_EXHAUSTED_WITH_UNRESOLVED_FILES','ATTEMPT_LIMIT','FILE_LIMIT','GPT_LIMIT','INSPECT_ONLY') else 2
    except KeyboardInterrupt:
        print('Прервано пользователем. Прогресс уже сохранён на диске.')
        return 130
    except Exception as exc:
        print('Остановка:',type(exc).__name__,str(exc)[:250])
        return 2
    finally:
        if wake_held:release_windows_awake()
        if playwright:playwright.stop()


if __name__=='__main__':
    sys.exit(main())
