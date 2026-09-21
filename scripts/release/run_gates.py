"""Release gates; default publish mode treats NOT_RUN as failure."""
from __future__ import annotations
import argparse, shutil, subprocess, sys, urllib.request, urllib.parse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
COMPOSE=ROOT/'compose.prod.yaml'; NGINX=ROOT/'deploy/nginx/musicdl.conf'; CADDY=ROOT/'deploy/caddy/Caddyfile'
def _python():
 p=ROOT/'.venv'/'Scripts'/'python.exe'
 if p.exists(): return p
 p=ROOT.parent.parent/'.venv'/'Scripts'/'python.exe'
 return p if p.exists() else Path(sys.executable)
def report(n,s,d): print(f'[{n}] {s} - {d}'); return s=='PASS'
def compose_contract(t=None):
 t=t or COMPOSE.read_text()
 cs=[('services:\n  musicdl:' in t and '  plugin-runner:' in t and '  admin-panel:' not in t,'two application services: the app, which serves the console, and the plugin runner'),('redis:' not in t and 'MUSICDL_REDIS__URL: "${MUSICDL_REDIS__URL:?' in t,'external Redis only'),(t.count('user: "10001:10001"')==2,'every service non-root'),(t.count('read_only: true')==2 and t.count('cap_drop: [ALL]')==2 and t.count('no-new-privileges:true')==2,'read-only root and privilege boundary'),(t.count('limits:')==2 and t.count('cpus:')==2 and t.count('memory:')==2,'hard CPU/memory limits'),(t.count('healthcheck:')==2 and 'internal: true' in t,'health probes and internal network')]
 return all(report('compose','PASS' if ok else 'FAIL',d) for ok,d in cs)
def compose(): return compose_contract()
def one_line_blocks(t):
 'Caddy opens a block on one line and closes it on another; a one-liner makes the whole file unadaptable.'
 return [ln.strip() for ln in t.splitlines() if ' {' in ln.split('#',1)[0] and '}' in ln.split('#',1)[0].split('{',1)[1]]
def proxy_contract(n,c):
 cs=[('return 308 https://' in n and 'listen 443 ssl' in n and 'ssl_protocols TLSv1.2 TLSv1.3' in n,'Nginx HTTPS/TLS'),('expires 7d' in n and 'location /api/' not in n and 'location / {' in n,'Nginx caches static assets and sends everything else -- console and API -- to the one process'),('access_log /var/log/nginx/access.log combined if=$release_loggable' in n and 'proxy_pass http://musicdl_app/wecom/callback;' in n and 'strip_query' not in n,'Nginx preserves callback query and skips its access log'),('redir https://{host}{uri} permanent' in c and 'tls /etc/caddy/tls' in c,'Caddy HTTPS/TLS'),('log_skip @callback' in c and 'uri strip_query' not in c and 'reverse_proxy musicdl:8000' in c,'Caddy preserves callback query and skips its log'),('Cache-Control' in c and 'handle {' in c and 'handle_path' not in c,'Caddy caches static assets and proxies everything else, with no second upstream to strip a path for'),(not one_line_blocks(c),'every Caddy block opens and closes on its own line')]; return all(report('proxy','PASS' if ok else 'FAIL',d) for ok,d in cs)
def proxy():
 n,c=NGINX.read_text(),CADDY.read_text(); return proxy_contract(n,c)
def telegram_contract(t):
 main,plug=t.split('  plugin-runner:',1); plug=plug.split('\nvolumes:',1)[0]; return main.count('musicdl-telegram:/data/telegram-sessions')==1 and 'musicdl-telegram' not in plug and 'SESSION' not in plug.upper()
def telegram():
 ok=telegram_contract(COMPOSE.read_text()); return report('telegram-session-isolation','PASS' if ok else 'FAIL','parsed service volume/env ownership')
def media():
 tests=sorted(str(p.relative_to(ROOT)) for p in (ROOT/'tests').glob('**/test_media_*.py')); py=_python()
 if not tests:return report('media-integrity','NOT_RUN','media tests absent')
 r=subprocess.run([str(py),'-m','pytest','-q','-W','error',*tests],cwd=ROOT,capture_output=True); return report('media-integrity','PASS' if r.returncode==0 else 'FAIL',f'pytest media tests exit {r.returncode}')
def plugin_contract(t):
 s=t.split('  plugin-runner:',1)[1].split('\n\nvolumes:',1)[0]; req=['user: "10001:10001"','read_only: true','cap_drop: [ALL]','no-new-privileges:true','limits:','cpus:','memory:','networks: [plugin-control]']; forbidden=['docker.sock','telegram','session','api_key','redis','secret','config']; return all(x in s for x in req) and not any(x in s.lower() for x in forbidden)
PLUGIN_SUITE=ROOT/'tests/integration/test_plugin_runtime.py'
def plugin():
 t=COMPOSE.read_text(); ok=plugin_contract(t)
 if not ok:return report('plugin-security','FAIL','plugin-runner static boundary violated in compose.prod.yaml')
 if not PLUGIN_SUITE.exists():return report('plugin-security','FAIL','mandatory hostile runtime suite is absent')
 if not shutil.which('docker'):return report('plugin-security','NOT_RUN','static boundary valid; no docker CLI on this host, so the mandatory hostile runtime suite cannot run here (last full run: .planning/phases/06-restricted-plugin-runtime/SUMMARY.md)')
 r=subprocess.run([str(_python()),'-m','pytest','-q','-rs','-p','no:cacheprovider',str(PLUGIN_SUITE)],cwd=ROOT,capture_output=True,text=True)
 out=(r.stdout or '')+(r.stderr or '')
 if r.returncode!=0:return report('plugin-security','FAIL',f'hostile runtime suite exit {r.returncode}')
 if 'skipped' in out:return report('plugin-security','NOT_RUN','hostile runtime suite skipped because Docker Engine is unavailable')
 return report('plugin-security','PASS','hostile runtime suite passed with no skips')
def admin():
 tests=sorted(str(p) for p in (ROOT/'tests/unit').glob('test_admin_*.py'))
 if not tests:return report('admin-auth','NOT_RUN','admin tests are not merged into this Worktree')
 py=_python()
 r=subprocess.run([str(py),'-m','pytest','-q','-W','error',*tests],cwd=ROOT); return report('admin-auth','PASS' if r.returncode==0 else 'FAIL',f'admin pytest exit {r.returncode}')
def wecom(url=None,expected=None):
 if not url:return report('wecom-callback','NOT_RUN','real callback requires explicit --wecom-url opt-in')
 parsed=urllib.parse.urlsplit(url)
 if parsed.scheme!='https' or parsed.username or parsed.password or expected is None:return report('wecom-callback','FAIL','probe requires HTTPS without userinfo and --wecom-expected')
 try:
  with urllib.request.urlopen(url,timeout=10) as r: body=r.read(4096).decode('utf-8','replace'); ok=r.status==int(expected) or body==expected
  return report('wecom-callback','PASS' if ok else 'FAIL','controlled HTTPS callback probe matched expected status/body')
 except Exception:return report('wecom-callback','FAIL','controlled callback probe failed (details suppressed)')
BACKUP_VOLUMES=('musicdl-media','musicdl-app-data','musicdl-telegram')
def backup_plan():
 'Exercise the real backup script; a placeholder cannot name every volume.'
 script=ROOT/'scripts/release/backup_restore.ps1'
 if not script.exists():return 'FAIL','backup script is absent'
 host=shutil.which('pwsh') or shutil.which('powershell')
 if not host:return 'NOT_RUN','no PowerShell host available for the backup dry-run'
 r=subprocess.run([host,'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-Action','dry-run'],cwd=ROOT,capture_output=True,text=True)
 out=(r.stdout or '')+(r.stderr or '')
 if r.returncode!=0:return 'FAIL',f'backup dry-run exit {r.returncode}'
 missing=[n for n in BACKUP_VOLUMES if n not in out]
 return ('FAIL','backup dry-run omits '+missing[0]) if missing else ('PASS',f'backup plan resolved for {len(BACKUP_VOLUMES)} volumes')
def recovery(redis_url=None,confirm=False,namespace=None):
 t=COMPOSE.read_text(); ok='restart: unless-stopped' in t and all(x in t for x in [n+':' for n in BACKUP_VOLUMES])
 if not ok:return report('compose-recovery','FAIL','restart/volume contract invalid')
 plan,detail=backup_plan()
 if plan!='PASS':return report('compose-recovery',plan,detail)
 if not redis_url or not confirm:return report('compose-recovery','NOT_RUN','backup drill resolved; isolated Redis recovery requires --redis-url and --confirm-isolated')
 ns=namespace or 'musicdl-gate-'+__import__('secrets').token_hex(6)
 r=subprocess.run([str(_python()),str(ROOT/'scripts/release/redis_recovery.py'),'--url',redis_url,'--namespace',ns,'--confirm-isolated'],cwd=ROOT); return report('compose-recovery','PASS' if r.returncode==0 else 'FAIL',detail+f'; isolated Redis recovery exit {r.returncode}')
FUN={'compose':compose,'proxy':proxy,'wecom-callback':wecom,'telegram-session-isolation':telegram,'media-integrity':media,'plugin-security':plugin,'admin-auth':admin,'compose-recovery':recovery}
def main():
 p=argparse.ArgumentParser(); p.add_argument('--gate',choices=['all',*FUN],default='all'); p.add_argument('--dry-run',action='store_true'); p.add_argument('--self-test',action='store_true'); p.add_argument('--wecom-url'); p.add_argument('--wecom-expected'); p.add_argument('--redis-url'); p.add_argument('--confirm-isolated',action='store_true'); p.add_argument('--namespace'); a=p.parse_args(); good=True
 if a.self_test:
  base=COMPOSE.read_text(); n=NGINX.read_text(); c=CADDY.read_text(); plug=base.replace('  plugin-runner:\n','  plugin-runner:\n    environment:\n      API_KEY: leaked\n',1); badtele=base.replace('  plugin-runner:\n','  plugin-runner:\n    volumes:\n      - musicdl-telegram:/bad\n',1); checks=[not compose_contract(base.replace('read_only: true','read_only: false',1)), not telegram_contract(badtele), not proxy_contract(n,c.replace('log_skip @callback','',1)), not plugin_contract(plug)]; good=all(checks); print('[self-test]', 'PASS' if good else 'FAIL','- compose, Telegram, proxy, and plugin tamper checks')
 for n in (FUN if a.gate=='all' else {a.gate:FUN[a.gate]}):
  ok=wecom(a.wecom_url,a.wecom_expected) if n=='wecom-callback' else recovery(a.redis_url,a.confirm_isolated,a.namespace) if n=='compose-recovery' else FUN[n](); good=good and (ok or (a.dry_run and n in {'wecom-callback','plugin-security','admin-auth','compose-recovery'}))
 return 0 if good else 1
if __name__=='__main__': raise SystemExit(main())
