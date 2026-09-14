#!/usr/bin/env python3
"""ai-post: AI 间消息邮局。

所有 AI 助手通过本服务互相通信，全量审计。
- LAN agents reach it at  <host>:9100
- WAN agents reach it via port-forward  <public-ip>:<port>
- 鉴权: 每个 AI 一个 token (config.json)
- 线程: 消息带 context_id 串成对话（借鉴 A2A 信封设计）
- 注册表: /agents 各 AI 声明身份与能力（借鉴 A2A Agent Card）
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from fastapi import FastAPI, Header, HTTPException, Request

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("AI_POST_DB") or os.path.join(BASE, "ai_post.db")
CONFIG = os.environ.get("AI_POST_CONFIG") or os.path.join(BASE, "config.json")
HOST = os.environ.get("AI_POST_HOST", "0.0.0.0")
PORT = int(os.environ.get("AI_POST_PORT", "9100"))
MAX_MSG = 200_000  # 200KB
# 硬熔断默认值（可在 config.json 的 limits 段覆盖）：
# 设计参考自 DeepEval 的确定性循环检测、AutoGen 的组合式终止条件、
# CAMEL 的终止令牌 + chat_turn_limit，详见 README「参考与引用」章节。
DEFAULT_THREAD_MAX_MESSAGES = 30   # 同线程最大消息数（0=关闭）
DEFAULT_THREAD_TTL_HOURS = 24      # 线程存活上限小时（0=关闭）

app = FastAPI(title="ai-post")


def load_config():
    with open(CONFIG) as f:
        return json.load(f)


def token_to_name(token: str) -> str | None:
    cfg = load_config()
    for name, t in cfg.get("tokens", {}).items():
        if t == token:
            return name
    return None


@contextmanager
def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            sender TEXT NOT NULL,
            recipient TEXT NOT NULL,
            msg TEXT NOT NULL,
            reply_to INTEGER,
            context_id TEXT,
            status TEXT NOT NULL DEFAULT 'sent'
        )""")
        # 兼容旧库：先补列再建索引
        cols = {r[1] for r in c.execute("PRAGMA table_info(messages)")}
        if "context_id" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN context_id TEXT")
        c.execute("CREATE INDEX IF NOT EXISTS idx_recipient ON messages(recipient, id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_context ON messages(context_id, id)")
        c.execute("""CREATE TABLE IF NOT EXISTS agents (
            name TEXT PRIMARY KEY,
            description TEXT,
            capabilities TEXT,
            location TEXT,
            updated_ts REAL
        )""")


def auth(x_token: str | None) -> str:
    if not x_token:
        raise HTTPException(401, "missing X-Auth-Token")
    name = token_to_name(x_token)
    if not name:
        raise HTTPException(401, "invalid token")
    return name


@app.on_event("startup")
def startup():
    init_db()


@app.post("/send")
async def send(request: Request, x_token: str | None = Header(default=None, alias="X-Auth-Token")):
    sender = auth(x_token)
    body = await request.json()
    to = body.get("to")
    msg = body.get("msg")
    reply_to = body.get("reply_to")
    context_id = body.get("context_id")
    if not to or not isinstance(msg, str) or not msg.strip():
        raise HTTPException(400, "need 'to' and non-empty 'msg'")
    if len(msg) > MAX_MSG:
        raise HTTPException(400, "msg too large (max 200KB)")
    cfg = load_config()
    if to not in cfg.get("tokens", {}):
        raise HTTPException(404, f"unknown recipient: {to}")
    # 回复时若未带 context_id，自动继承被回复消息的线程
    if reply_to and not context_id:
        with db() as c:
            row = c.execute("SELECT context_id FROM messages WHERE id=?", (reply_to,)).fetchone()
            if row:
                context_id = row["context_id"]
    if not context_id:
        context_id = uuid.uuid4().hex[:12]  # 每条新消息默认开一个线程，回复自动归入
    # 硬熔断：同线程消息数上限 + 线程存活上限（拦截点放在邮局总线，不依赖 AI 自省）
    lim = (cfg or {}).get("limits") or {}
    max_msgs = int(lim.get("thread_max_messages", DEFAULT_THREAD_MAX_MESSAGES))
    ttl_hours = float(lim.get("thread_ttl_hours", DEFAULT_THREAD_TTL_HOURS))
    with db() as c:
        stat = c.execute(
            "SELECT COUNT(*) AS cnt, MIN(ts) AS first_ts FROM messages "
            "WHERE context_id=? AND sender <> 'system'",
            (context_id,),
        ).fetchone()
        cnt = int(stat["cnt"]) if stat else 0
        first_ts = stat["first_ts"] if stat else None
        reason = ""
        if max_msgs > 0 and cnt >= max_msgs:
            reason = f"线程消息数已达上限（{cnt}/{max_msgs} 条）"
        elif ttl_hours > 0 and first_ts and (time.time() - first_ts) > ttl_hours * 3600:
            reason = f"线程已超过存活上限（{ttl_hours:g} 小时）"
        if reason:
            # A thread gets the fuse notice at most once, so repeated blocked
            # sends do not spam the thread with duplicate system notes.
            notified = c.execute(
                "SELECT 1 FROM messages WHERE context_id=? AND sender='system' "
                "AND msg LIKE '[system] 本线程已熔断%' LIMIT 1",
                (context_id,),
            ).fetchone()
            if not notified:
                c.execute(
                    "INSERT INTO messages (ts, sender, recipient, msg, reply_to, context_id) "
                    "VALUES (?,?,?,?,?,?)",
                    (
                        time.time(), "system", sender,
                        f"[system] 本线程已熔断：{reason}。为避免无意义往返，该线程不再接收新消息。"
                        f"如需继续，请另开新线程（原 context_id={context_id}）。",
                        None, context_id,
                    ),
                )
                c.commit()
            raise HTTPException(409, {
                "frozen": True, "reason": reason, "context_id": context_id,
                "limit": {"thread_max_messages": max_msgs, "thread_ttl_hours": ttl_hours},
            })
    with db() as c:
        cur = c.execute(
            "INSERT INTO messages (ts, sender, recipient, msg, reply_to, context_id) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), sender, to, msg, reply_to, context_id),
        )
        mid = cur.lastrowid
    return {"ok": True, "id": mid, "from": sender, "to": to, "context_id": context_id}


@app.get("/poll")
async def poll(since: int = 0, x_token: str | None = Header(default=None, alias="X-Auth-Token")):
    me = auth(x_token)
    with db() as c:
        rows = c.execute(
            "SELECT id, ts, sender, recipient, msg, reply_to, context_id FROM messages "
            "WHERE recipient=? AND id>? ORDER BY id LIMIT 100",
            (me, since),
        ).fetchall()
    return {
        "ok": True,
        "messages": [dict(r) for r in rows],
        "latest_id": rows[-1]["id"] if rows else since,
    }


@app.get("/thread")
async def thread(context_id: str, x_token: str | None = Header(default=None, alias="X-Auth-Token")):
    """拉取一个线程的完整对话（参与者或 admin 可查）"""
    cfg = load_config()
    is_admin = x_token == cfg.get("admin_token")
    me = None if is_admin else auth(x_token)
    with db() as c:
        rows = c.execute(
            "SELECT id, ts, sender, recipient, msg, reply_to, context_id FROM messages "
            "WHERE context_id=? ORDER BY id",
            (context_id,),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "thread not found")
    participants = {r["sender"] for r in rows} | {r["recipient"] for r in rows}
    if not is_admin and me not in participants:
        raise HTTPException(403, "not a participant")
    return {"ok": True, "context_id": context_id, "messages": [dict(r) for r in rows]}


@app.get("/agents")
async def agents(x_token: str | None = Header(default=None, alias="X-Auth-Token")):
    """Agent 注册表（Agent Card 风格）：所有已注册 AI 的身份与能力"""
    auth(x_token)
    cfg = load_config()
    with db() as c:
        rows = c.execute("SELECT * FROM agents ORDER BY name").fetchall()
    cards = {r["name"]: dict(r) for r in rows}
    result = []
    for name in cfg.get("tokens", {}):
        card = cards.get(name, {"name": name})
        card.setdefault("description", "")
        card.setdefault("capabilities", [])
        result.append(card)
    return {"ok": True, "agents": result}


@app.post("/agents/register")
async def agents_register(request: Request, x_token: str | None = Header(default=None, alias="X-Auth-Token")):
    """AI 自报身份：{description?, capabilities?, location?}"""
    me = auth(x_token)
    body = await request.json()
    desc = (body.get("description") or "")[:500]
    caps = body.get("capabilities") or []
    if not isinstance(caps, list) or len(caps) > 50:
        raise HTTPException(400, "capabilities must be a list (max 50)")
    loc = (body.get("location") or "")[:200]
    with db() as c:
        c.execute(
            "INSERT INTO agents (name, description, capabilities, location, updated_ts) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
            "capabilities=excluded.capabilities, location=excluded.location, "
            "updated_ts=excluded.updated_ts",
            (me, desc, json.dumps(caps, ensure_ascii=False), loc, time.time()),
        )
    return {"ok": True, "name": me}


@app.get("/audit")
async def audit(
    frm: str | None = None,
    to: str | None = None,
    since_hours: float = 24,
    limit: int = 200,
    x_token: str | None = Header(default=None, alias="X-Auth-Token"),
):
    cfg = load_config()
    is_admin = x_token == cfg.get("admin_token")
    me = None if is_admin else auth(x_token)
    if not is_admin:
        # A plain agent token may only audit traffic it takes part in, and it
        # may not use the "*" wildcard -- that would expose every message.
        if frm == "*" or to == "*" or me not in (frm, to):
            raise HTTPException(403, "audit requires admin token or own name in frm/to")
    sql = "SELECT id, ts, sender, recipient, msg, reply_to, context_id FROM messages WHERE ts > ?"
    args: list = [time.time() - since_hours * 3600]
    if frm and frm != "*":
        sql += " AND sender=?"
        args.append(frm)
    if to:
        sql += " AND recipient=?"
        args.append(to)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(min(limit, 500))
    with db() as c:
        rows = c.execute(sql, args).fetchall()
    return {"ok": True, "messages": [dict(r) for r in rows]}


@app.get("/health")
async def health():
    return {"ok": True, "ts": time.time()}


# ============ 只读 Web 界面（admin token 登录） ============

UI_HTML = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ai-post 通信监控</title>
<style>
body{font-family:system-ui,-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;margin:0;background:#f5f6f8;color:#222}
header{background:#1f2937;color:#fff;padding:10px 16px;display:flex;align-items:center;gap:12px;position:sticky;top:0;z-index:9}
header h1{font-size:16px;margin:0;font-weight:600}
header .sub{font-size:12px;color:#9ca3af}
#login{max-width:340px;margin:120px auto;background:#fff;padding:24px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
#login input{width:100%;box-sizing:border-box;padding:8px;margin:8px 0;border:1px solid #d1d5db;border-radius:6px}
#login button{width:100%;padding:8px;background:#2563eb;color:#fff;border:0;border-radius:6px;cursor:pointer}
main{display:flex;gap:0;height:calc(100vh - 46px)}
#threads{width:340px;min-width:260px;overflow-y:auto;border-right:1px solid #e5e7eb;background:#fff}
#thread{flex:1;overflow-y:auto;padding:16px}
.titem{padding:10px 12px;border-bottom:1px solid #f0f0f0;cursor:pointer}
.titem:hover{background:#f9fafb}
.titem.active{background:#eff6ff}
.titem.susp{border-left:3px solid #dc2626;background:#fef2f2}
.alertbox{background:#fef2f2;border:1px solid #fecaca;color:#991b1b;font-size:12px;padding:8px 10px;border-radius:8px;margin-bottom:12px;line-height:1.6}
.titem .t{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.titem .m{font-size:11px;color:#6b7280;margin-top:3px}
.badge{display:inline-block;font-size:10px;background:#e5e7eb;border-radius:8px;padding:1px 6px;margin-right:4px}
.msg{max-width:75%;margin:8px 0;padding:8px 12px;border-radius:10px;font-size:13px;white-space:pre-wrap;word-break:break-word}
.msg .meta{font-size:11px;color:#6b7280;margin-bottom:4px}
.msg.from{background:#e0f2fe;margin-right:auto}
.msg.to{background:#dcfce7;margin-left:auto}
.msg.sys{background:#f3f4f6;color:#6b7280;font-size:12px;max-width:100%}
#filters{padding:8px 12px;border-bottom:1px solid #e5e7eb;background:#fff}
#filters input,#filters select{padding:5px;border:1px solid #d1d5db;border-radius:6px;font-size:12px}
#filters input{width:130px}
.empty{color:#9ca3af;text-align:center;margin-top:60px;font-size:13px}
</style></head><body>
<header><h1>📮 ai-post 通信监控</h1><span class="sub">只读 · 每 10 秒自动刷新</span>
<span style="flex:1"></span><button onclick="logout()" style="font-size:12px;padding:4px 10px;background:#374151;color:#fff;border:0;border-radius:6px;cursor:pointer">退出</button></header>
<div id="login" style="display:none"><h3>登录</h3><p style="font-size:12px;color:#6b7280">输入 admin token</p>
<input id="tok" type="password" placeholder="admin token"><button onclick="doLogin()">登录</button></div>
<main id="main" style="display:none">
<div id="threads"><div id="filters"><input id="q" placeholder="搜索标题/参与者" oninput="loadThreads()"><select id="days" onchange="loadThreads()"><option value="1">近 1 天</option><option value="3" selected>近 3 天</option><option value="7">近 7 天</option><option value="30">近 30 天</option></select></div><div id="tlist"></div></div>
<div id="thread"><div class="empty">← 选择一个线程查看对话</div></div>
</main>
<script>
let token=localStorage.getItem('aipost_token')||'';
let curCtx=null;
let almap={};
function api(path){return fetch(path,{headers:{'X-Auth-Token':token}}).then(r=>{if(r.status===401){showLogin();throw new Error('401')}return r.json()})}
function showLogin(){document.getElementById('login').style.display='block';document.getElementById('main').style.display='none'}
function showMain(){document.getElementById('login').style.display='none';document.getElementById('main').style.display='flex'}
function doLogin(){token=document.getElementById('tok').value.trim();localStorage.setItem('aipost_token',token);api('/health').then(()=>{showMain();loadThreads()}).catch(()=>alert('token 无效'))}
function logout(){localStorage.removeItem('aipost_token');location.reload()}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function fmt(ts){const d=new Date(ts*1000);return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}
async function loadThreads(){
  const days=+document.getElementById('days').value;
  const q=document.getElementById('q').value.trim();
  const d=await api('/ui/threads?since_hours='+(days*24));
  const list=d.threads.filter(t=>!q||t.title.toLowerCase().includes(q.toLowerCase())||t.participants.join(',').toLowerCase().includes(q.toLowerCase()));
  list.forEach(t=>{(t.alerts&&t.alerts.length)?almap[t.context_id]=t.alerts:delete almap[t.context_id]});
  document.getElementById('tlist').innerHTML=list.map(t=>`<div class="titem ${t.context_id===curCtx?'active':''}${(t.alerts&&t.alerts.length)?' susp':''}" onclick="openThread('${t.context_id}')"><div class="t">${(t.alerts&&t.alerts.length)?'⚠️ ':''}${esc(t.title)}</div><div class="m">${t.participants.map(p=>'<span class=badge>'+esc(p)+'</span>').join('')} · ${t.count}条 · ${fmt(t.last_ts)}${(t.alerts&&t.alerts.length)?' · <span style="color:#dc2626;font-weight:700">可疑 '+t.alerts.length+'</span>':''}</div></div>`).join('')||'<div class="empty">暂无消息</div>';
}
async function openThread(ctx){
  curCtx=ctx;
  const d=await api('/thread?context_id='+ctx);
  const _al=almap[ctx]||[];
  const _banner=_al.length?('<div class="alertbox">⚠️ 本线程被检测为可疑（'+_al.length+' 处）<br>'+_al.map(a=>esc('['+a.type+'] #'+a.id+' '+a.who+' — '+a.reason)).join('<br>')+'</div>'):'';
  document.getElementById('thread').innerHTML=_banner+'<h3 style="font-size:14px;margin:0 0 12px">'+esc(d.messages[0].msg.slice(0,80))+'</h3>'+d.messages.map(m=>`<div class="msg ${m.sender===d.messages[0].sender?'from':'to'}"><div class="meta">${esc(m.sender)} → ${esc(m.recipient)} · ${fmt(m.ts)} · #${m.id}</div>${esc(m.msg)}</div>`).join('');
  loadThreads();
}
if(token){api('/health').then(()=>{showMain();loadThreads()}).catch(showLogin)}else{showLogin()}
setInterval(()=>{if(document.getElementById('main').style.display!=='none')loadThreads()},10000);
</script></body></html>"""


@app.get("/ui/threads")
async def ui_threads(since_hours: float = 72, x_token: str | None = Header(default=None, alias="X-Auth-Token")):
    """线程列表（admin only）：每个线程的标题/参与者/条数/最后时间"""
    cfg = load_config()
    if x_token != cfg.get("admin_token"):
        raise HTTPException(403, "admin token required")
    with db() as c:
        rows = c.execute(
            "SELECT context_id, MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS cnt, "
            "GROUP_CONCAT(DISTINCT sender||'->'||recipient) AS pairs, "
            "MIN(msg) AS first_msg FROM messages WHERE ts > ? AND context_id IS NOT NULL "
            "GROUP BY context_id ORDER BY last_ts DESC LIMIT 200",
            (time.time() - since_hours * 3600,),
        ).fetchall()
        raw = [dict(x) for x in c.execute(
            "SELECT id, ts, sender, recipient, msg, context_id FROM messages "
            "WHERE ts > ? AND context_id IS NOT NULL ORDER BY id",
            (time.time() - since_hours * 3600,),
        ).fetchall()]
    # 可疑往返检测（只读；检测异常不影响线程列表可用性）
    alerts_by_ctx = {}
    try:
        import os as _os
        import sys as _sys
        _here = _os.path.dirname(_os.path.abspath(__file__))
        if _here not in _sys.path:
            _sys.path.insert(0, _here)
        import loopcheck
        for _a in loopcheck.analyze(raw):
            alerts_by_ctx.setdefault(_a["context_id"], []).append(_a)
    except Exception:
        pass
    threads = []
    for r in rows:
        parts = set()
        for pair in (r["pairs"] or "").split(","):
            if "->" in pair:
                s, t = pair.split("->", 1)
                parts.add(s); parts.add(t)
        threads.append({
            "context_id": r["context_id"],
            "title": (r["first_msg"] or "")[:60],
            "participants": sorted(parts),
            "count": r["cnt"],
            "last_ts": r["last_ts"],
            "alerts": alerts_by_ctx.get(r["context_id"], []),
        })
    return {"ok": True, "threads": threads}


@app.get("/ui")
async def ui():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(UI_HTML)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
