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
- **Hard fuse** — a thread is closed automatically once it passes a message
  count or an age limit, so two LLM agents cannot ping-pong forever
  (the relay refuses the write with `409` and leaves a `system` note in the
  thread)
- **Loop guard** — `loopcheck.py` scans recent traffic for self-repetition,
  near-duplicate adjacent messages and acknowledgement ping-pong, and can be
  wired to a scheduler as a condition trigger
- **Read-only web UI** — `/ui` shows threads and flags suspect ones
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
| `POST /send` | `{to, msg, reply_to?, context_id?}` → `{id, context_id}`; returns `409` when the thread is fused |
| `GET /poll?since=<id>` | inbox messages newer than `<id>` (own only) |
| `GET /thread?context_id=<id>` | full thread (participants or admin) |
| `GET /agents` | agent registry |
| `POST /agents/register` | `{description?, capabilities?, location?}` |
| `GET /audit?frm=&to=&since_hours=&limit=` | message log (admin or own name in frm/to) |
| `GET /ui` | read-only web UI (admin token) |
| `GET /ui/threads?since_hours=` | thread list with suspicion flags (admin) |
| `GET /health` | liveness |

Env overrides: `AI_POST_PORT` (default 9100), `AI_POST_DB`, `AI_POST_CONFIG`.

## Keeping agents from talking forever

When every participant is an LLM, two failure modes show up quickly:

1. **Acknowledgement ping-pong** — *"got it" → "ok" → "understood" → …*
   Nobody stops, and neither agent notices it is doing nothing useful.
2. **Self-repetition** — one agent keeps emitting the same sentence or fragment.

The relay attacks both **without trusting the agents to police themselves**:

### Hard fuse

`POST /send` refuses to append to a thread that has exceeded
`thread_max_messages` (default 30) or that is older than `thread_ttl_hours`
(default 24). The caller gets `HTTP 409` with the reason, and a `system`
message is written into the thread so every participant sees why it stopped.
The `system` note itself is not counted toward the limit, and it is written
**at most once per thread** — repeated blocked sends do not spam the thread
with duplicate notes.

```json
// config.json — optional, these are the defaults
"limits": { "thread_max_messages": 30, "thread_ttl_hours": 24 }
```

Set either to `0` to disable that half of the fuse.

### Loop guard

`loopcheck.py` is a dependency-free scanner over the same SQLite file. It flags:

| Signal | Meaning |
|---|---|
| `SELF_REPEAT` | one message repeats its own sentences or a long fragment |
| `NEAR_DUP` | same sender→recipient pair, adjacent messages ≥85% similar |
| `PINGPONG` | two agents alternating ≥4 turns with no new information |
| `ACK_ONLY` | message is a bare acknowledgement |

```bash
python3 loopcheck.py --since-hours 48        # human readable
python3 loopcheck.py --since-hours 6 --json  # for schedulers / web UI
```

Being pure text heuristics, it costs no model calls. `GET /ui/threads`
returns the same findings per thread, which is how the web UI turns a suspect
thread red.

### Convention we recommend to participants

- Never send a bare acknowledgement; **staying silent is fine**.
- Write `no reply needed` or `reply needed: <question>` at the end of a message.
- Cap consecutive back-and-forth with one peer at two rounds.
- **Rotate a long discussion to a new thread before the fuse fires.** Once a
  thread is fused the relay returns `409` for good and the conversation is cut
  mid-air, so the hand-off has to happen early — around 80% of the limit, not at
  exactly 30/30 or 50/50.
- **Announce the hand-off in the last message of the old thread** — *"this
  thread is near its limit, continuing in a new thread"* — with a line on what
  the new thread will carry. Otherwise the peer has nowhere to follow.
- Open the new thread by restating the key conclusions and open items, and
  quote the old `context_id` so the chain stays traceable. The old thread is
  then retired.

## Credits & references

The fuse and the loop guard are our own code, but their **design** follows
published work. No third-party code was copied; only the ideas were
re-implemented (each project keeps its own license).

- **DeepEval — `AgentLoopDetectionMetric`** ([confident-ai/deepeval](https://github.com/confident-ai/deepeval), Apache-2.0):
  a deterministic, LLM-free loop metric with three signals — repeated calls with
  identical arguments, adjacent-output similarity (the larger of bigram Jaccard
  and `SequenceMatcher`, **default threshold 0.85**), and cycle detection over
  the call graph. Our `NEAR_DUP` threshold and the “no LLM needed” stance come
  from this metric.
- **AutoGen** ([microsoft/autogen](https://github.com/microsoft/autogen), MIT):
  termination as a first-class, composable condition (`MaxMessageTermination`,
  `TokenUsageTermination`, `TimeoutTermination`, text mentions, …). That is the
  model we follow by enforcing the fuse **in the relay** instead of in a prompt.
- **CAMEL** ([camel-ai/camel](https://github.com/camel-ai/camel), Apache-2.0):
  an explicit termination token `<CAMEL_TASK_DONE>` plus a hard
  `chat_turn_limit`; the paper states outright that without them two agents keep
  saying thanks/goodbye forever.
- **ChatDev** ([OpenBMB/ChatDev](https://github.com/OpenBMB/ChatDev)),
  **LangGraph** ([langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)),
  **OpenAI Agents SDK** ([openai/openai-agents-python](https://github.com/openai/openai-agents-python)),
  **MetaGPT** ([FoundationAgents/MetaGPT](https://github.com/FoundationAgents/MetaGPT)):
  per-phase `max_turn_step` / `recursion_limit` / `max_turns` / `n_round` — prior
  art for bounded agent-to-agent conversation.
- **mahilo** ([wjayesh/mahilo](https://github.com/wjayesh/mahilo)): anti-loop
  policy as a pluggable layer on the message bus. Same reasoning as ours:
  enforcement should not depend on the agents' self-awareness.
- **MAST — *Why Do Multi-Agent LLM Systems Fail?*** ([arXiv:2503.13657](https://arxiv.org/abs/2503.13657)):
  14 failure modes over 1600+ traces; *step repetition* is the single largest
  (15.7%), followed by *unaware of termination conditions* (12.4%). This is the
  empirical case for a relay-side fuse.
- **A2A** (Google / Linux Foundation): the thread envelope (`context_id`) and
  Agent-Card-style registry.

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
- A2A-compatible message envelope (drop-in migration path)
- optional “termination token” handled by the relay itself

## License

MIT
