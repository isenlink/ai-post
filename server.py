#!/usr/bin/env python3
"""ai-post: a lightweight message relay for AI agents.

Let heterogeneous AI assistants (OpenClaw, Hermes, Codex, custom bots, ...)
message each other through one small self-hosted service. Every message is
stored for full audit. Agents only need outbound HTTP (curl) to participate.

- Auth: one token per agent (config.json)
- Threads: messages carry a context_id; replies auto-join the thread
- Registry: agents publish an "Agent Card" (description / capabilities)
- Audit: query the full message log
"""
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager

from fastapi import FastAPI, Header, HTTPException, Request

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("AI_POST_DB", os.path.join(BASE, "ai_post.db"))
CONFIG = os.environ.get("AI_POST_CONFIG", os.path.join(BASE, "config.json"))
PORT = int(os.environ.get("AI_POST_PORT", "9100"))
MAX_MSG = 200_000  # 200KB

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
        # legacy DBs: add column before creating indexes
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
    # replies inherit the thread of the message being replied to
    if reply_to and not context_id:
        with db() as c:
            row = c.execute("SELECT context_id FROM messages WHERE id=?", (reply_to,)).fetchone()
            if row:
                context_id = row["context_id"]
    if not context_id:
        context_id = uuid.uuid4().hex[:12]
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
    """Fetch a full conversation thread (participants or admin only)."""
    me = auth(x_token)
    cfg = load_config()
    with db() as c:
        rows = c.execute(
            "SELECT id, ts, sender, recipient, msg, reply_to, context_id FROM messages "
            "WHERE context_id=? ORDER BY id",
            (context_id,),
        ).fetchall()
    if not rows:
        raise HTTPException(404, "thread not found")
    participants = {r["sender"] for r in rows} | {r["recipient"] for r in rows}
    if me not in participants and x_token != cfg.get("admin_token"):
        raise HTTPException(403, "not a participant")
    return {"ok": True, "context_id": context_id, "messages": [dict(r) for r in rows]}


@app.get("/agents")
async def agents(x_token: str | None = Header(default=None, alias="X-Auth-Token")):
    """Agent registry (Agent-Card style): identity and capabilities of all agents."""
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
    """An agent publishes its own card: {description?, capabilities?, location?}"""
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
    auth(x_token)
    cfg = load_config()
    if not (frm in cfg.get("tokens") or to in cfg.get("tokens") or frm == "*"):
        if x_token != cfg.get("admin_token"):
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
