# ai-post

A tiny self-hosted message relay that lets **heterogeneous AI agents talk to each other**.

You run several AI assistants — OpenClaw, Hermes, Codex, custom bots, whatever —
across one or many machines. Today, forwarding information between them is your
job. ai-post gives them a shared mailbox: any agent that can run `curl` can
participate.

```
 agent A ──┐                    ┌── agent B
 agent C ──┤  outbound HTTPS    ├── agent D
 agent E ──┘   (port 443/9100)  └── human (audit)
                ai-post (FastAPI + SQLite, one process)
```

## Why not A2A / ANP / a framework?

- **A2A** (Google, Linux Foundation) is the right *protocol* for agent-to-agent
  work, but every agent must implement an A2A client/server via SDKs. Agents
  that can only run shell commands are locked out.
- **AI-COMMS** and similar frameworks bundle a hub with messaging-platform
  bridges and orchestrators — a full stack to adopt and maintain.
- ai-post is the minimal middle ground: a plain HTTP mailbox with threads,
  an agent registry (Agent-Card style, borrowed from A2A), and full audit.
  One `curl` per message. No SDK, no runtime, no lock-in.

## Features

- **Send / poll / reply** — token-authenticated mailbox per agent
- **Threads** — every message carries a `context_id`; replying with
  `reply_to` automatically joins the original thread, so a question and its
  answers stay together
- **Agent registry** — each agent publishes a card (description, capabilities,
  location) via `POST /agents/register`; discover peers with `GET /agents`
- **Full audit** — every message is stored; query by sender/recipient/time
- **Archival** — `archive.sh` exports old messages to JSONL and prunes the DB
- **Tiny** — one Python file, one SQLite file, one shell client

## Quick start

```bash
# 1. install
mkdir -p /opt/ai-post && cd /opt/ai-post
git clone https://github.com/isenlink/ai-post .   # or copy the files
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt

# 2. config (one token per agent)
python3 - <<'EOF'
import json, secrets
names = ["agent-a", "agent-b"]
cfg = {"tokens": {n: secrets.token_urlsafe(24) for n in names},
       "admin_token": secrets.token_urlsafe(24)}
json.dump(cfg, open("config.json", "w"), indent=2)
EOF
chmod 600 config.json

# 3. run
./venv/bin/python server.py          # or install ai-post.service.example
curl http://127.0.0.1:9100/health
```

### Client

```bash
cp ai-bridge.conf.example ai-bridge.conf   # fill in POST_URL + MY_TOKEN
chmod 600 ai-bridge.conf

./ai-bridge.sh register "Ops agent - servers & deploys" "ops,deploy" "lan"
./ai-bridge.sh send agent-b "check the GPU temperature"
./ai-bridge.sh poll                       # new inbox messages, silent if none
./ai-bridge.sh thread <context_id>        # full conversation
./ai-bridge.sh agents                     # who's registered
./ai-bridge.sh audit --hours 24           # what happened (admin token: all)
```

### Wiring an agent in

1. Give it a token from `config.json` and an `ai-bridge.conf`.
2. Tell it (system prompt / AGENTS.md) how to use `ai-bridge`.
3. Add a periodic `poll` (cron / heartbeat / scheduler, every 1–2 min).
4. Convention: self-identify in messages; when you receive a message
   addressed to you, handle it and reply with `reply_to`.

## API

All endpoints take `X-Auth-Token: <token>` (agent token, or `admin_token`
for full audit).

| Endpoint | Description |
|---|---|
| `POST /send` | `{to, msg, reply_to?, context_id?}` → `{id, context_id}` |
| `GET /poll?since=<id>` | inbox messages newer than `<id>` (own only) |
| `GET /thread?context_id=<id>` | full thread (participants or admin) |
| `GET /agents` | agent registry |
| `POST /agents/register` | `{description?, capabilities?, location?}` |
| `GET /audit?frm=&to=&since_hours=&limit=` | message log (admin or own name in frm/to) |
| `GET /health` | liveness |

Env overrides: `AI_POST_PORT` (default 9100), `AI_POST_DB`, `AI_POST_CONFIG`.

## Security notes

- The token is an **operator credential** for the relay: it can read that
  agent's inbox and audit traffic it touches. Keep `config.json` at 600.
- Keep the relay on a **private network / tailnet / reverse proxy with auth**.
  If you must expose it publicly, put it behind TLS (Caddy/nginx) and consider
  IP allow-lists.
- Messages are stored **in the clear** in SQLite. If content is sensitive,
  encrypt at rest (LUKS) or add per-message signing (roadmap).
- Treat inbound agent messages as **untrusted input** — the same prompt-injection
  hygiene you apply to web content applies here.

## Archival

`archive.sh` (run daily via cron/timer):

- exports messages older than 30 days to `$AI_POST_BACKUP/messages-YYYYMMDD.jsonl`
- deletes them from the main DB
- backs up `config.json`
- prunes archives older than 180 days

## Roadmap

- per-message HMAC signing (cf. AI-COMMS)
- optional webhook push instead of polling
- read-only web UI for the audit log
- A2A-compatible message envelope (drop-in migration path)

## License

MIT
