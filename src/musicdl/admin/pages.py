"""The HTML the administration portal serves to a browser.

``portal.py`` owns the JSON interface a console is written against; these pages
are what a person reaches first, before any session exists. They are built here
instead of read from a template file so that a deployment cannot lose its only
way in to a missing asset: the login page is the one page that must render even
when nothing else works.
"""

from __future__ import annotations

from html import escape


_STYLE = """
:root { color-scheme: dark; --ink:#f4f6fb; --muted:#9aa4bf; --line:rgba(255,255,255,.14);
  --field:rgba(255,255,255,.07); --accent:#7cc4ff; --danger:#ff9aa2; }
* { box-sizing: border-box; }
body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
  font:15px/1.6 "Noto Sans SC","PingFang SC","Microsoft YaHei",system-ui,sans-serif; color:var(--ink);
  background: radial-gradient(120% 120% at 12% 8%, #26365c 0%, #131726 45%, #0a0c14 100%); }
.card { width:min(400px,100%); padding:32px; border-radius:20px; border:1px solid var(--line);
  background:rgba(20,25,40,.55); backdrop-filter:blur(22px) saturate(140%);
  -webkit-backdrop-filter:blur(22px) saturate(140%); box-shadow:0 24px 60px rgba(0,0,0,.45); }
h1 { margin:0 0 4px; font-size:20px; letter-spacing:.02em; }
p.sub { margin:0 0 22px; color:var(--muted); font-size:13px; }
label { display:block; margin:0 0 14px; font-size:13px; color:var(--muted); }
input { width:100%; margin-top:6px; padding:11px 13px; color:var(--ink); font-size:15px;
  border-radius:12px; border:1px solid var(--line); background:var(--field); }
input:focus { outline:2px solid var(--accent); outline-offset:1px; }
button { width:100%; margin-top:8px; padding:12px; font-size:15px; font-weight:600; cursor:pointer;
  color:#08111f; border:0; border-radius:12px; background:linear-gradient(135deg,#8ed0ff,#6aa8ff); }
button:hover { filter:brightness(1.06); }
.error { margin:0 0 16px; padding:10px 12px; border-radius:12px; font-size:13px;
  color:var(--danger); border:1px solid rgba(255,122,138,.35); background:rgba(255,122,138,.1); }
.hint { margin:14px 0 0; color:var(--muted); font-size:12px; }
"""


def page(*, title: str, body: str) -> str:
    """One self-contained page: no build step, no external asset, no script."""
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{escape(title)}</title><style>{_STYLE}</style></head>'
            f'<body><main class="card">{body}</main></body></html>')


def credentials_form(*, csrf: str, error: str = "") -> str:
    """The forced credential change, as a form the dashboard can embed.

    The default ``admin``/``password`` pair is the only credential a fresh
    deployment has, and every route except the dashboard answers 403 until it is
    replaced, so the operator needs a working form the moment they log in. The
    token travels in the double-submitted field because a form post cannot carry
    the ``x-csrf-token`` header the JSON routes use.
    """
    alert = f'<p class="error">{escape(error)}</p>' if error else ""
    return (f'{alert}<form method="post" action="/admin/change-credentials-form">'
            f'<input type="hidden" name="csrf_token" value="{escape(csrf or "", quote=True)}">'
            '<label>当前密码<input name="password" type="password" autocomplete="current-password" required></label>'
            '<label>新用户名<input name="username" autocomplete="username" required></label>'
            '<label>新密码（至少 8 位）<input name="new_password" type="password" autocomplete="new-password" required minlength="8"></label>'
            '<button type="submit">保存并进入后台</button></form>')


def login_page(*, error: str = "") -> str:
    """The way into the panel, served to anyone who reaches ``/admin/``."""
    alert = f'<p class="error">{escape(error)}</p>' if error else ""
    body = (f'{alert}<form method="post" action="/admin/login-form">'
            '<label>用户名<input name="username" autocomplete="username" required autofocus></label>'
            '<label>密码<input name="password" type="password" autocomplete="current-password" required></label>'
            '<button type="submit">登录</button></form>'
            '<p class="hint">musicdl 管理后台 · 仅供个人使用</p>')
    return page(title="musicdl 管理后台 · 登录", body=f'<h1>musicdl 管理后台</h1>'
                '<p class="sub">请登录以管理音源、Bot 与容器配置。</p>' + body)


def credentials_page(*, csrf: str, error: str = "") -> str:
    """The credential change on its own, for a form post that was refused."""
    body = ('<h1>musicdl 管理后台</h1>'
            '<p class="sub">默认凭据必须先更换，之后才能使用其它后台接口。</p>'
            + credentials_form(csrf=csrf, error=error))
    return page(title="musicdl 管理后台 · 修改凭据", body=body)
