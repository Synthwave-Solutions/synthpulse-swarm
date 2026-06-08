# Hermes Swarm — System Architecture

> A self-hostable P2P multi-agent swarm server with real-time monitoring, built on the [Hermes Agent](https://github.com/hermes-agent) runtime.

---

## Table of Contents

1. [High-Level Overview](#1-high-level-overview)
2. [Package Structure](#2-package-structure)
3. [Startup & Lifecycle](#3-startup--lifecycle)
4. [Agent Daemon](#4-agent-daemon)
5. [Task Queue](#5-task-queue)
6. [Communication — Peer Messaging](#6-communication--peer-messaging)
7. [Prompt Architecture](#7-prompt-architecture)
8. [Tool System](#8-tool-system)
9. [Observability Stack (5 Layers)](#9-observability-stack-5-layers)
10. [Browser Pool](#10-browser-pool)
11. [Model Configuration](#11-model-configuration)
12. [Cron Scheduler](#12-cron-scheduler)
13. [Security](#13-security)
14. [Persistence & Data Layout](#14-persistence--data-layout)
15. [WebSocket Real-Time Layer](#15-websocket-real-time-layer)
16. [Deployment](#16-deployment)

---

## 1. High-Level Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        EXTERNAL WORLD                           │
│  Human Operator    LLM Backend (OpenAI-compat)    Dashboard UI  │
│    (HTTP/WS)       (configurable endpoint)         (Browser)    │
└──────┬──────────────────┬────────────────────────────┬──────────┘
       │                  │                            │
       ▼                  │                            ▼
┌──────────────┐          │          ┌──────────────────────────┐
│  FastAPI     │          │          │  WebSocket Broadcaster   │
│  REST API    │          │          │  (real-time dashboard)   │
│  + Auth      │          │          └────────────┬─────────────┘
└──────┬───────┘          │                       │
       │                  │                       │
       ▼                  ▼                       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    ORCHESTRATION LAYER                           │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ AgentDaemon  │  │ AgentDaemon  │  │ AgentDaemon  │  ...     │
│  │ (agent-a)    │  │ (agent-b)    │  │ (supervisor) │          │
│  │              │  │              │  │              │          │
│  │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │
│  │ │ SQLite   │ │  │ │ SQLite   │ │  │ │ SQLite   │ │          │
│  │ │ TaskQueue│ │  │ │ TaskQueue│ │  │ │ TaskQueue│ │          │
│  │ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │          │
│  │ ┌──────────┐ │  │ ┌──────────┐ │  │ ┌──────────┐ │          │
│  │ │ Thread   │ │  │ │ Thread   │ │  │ │ Thread   │ │          │
│  │ │ Executor │ │  │ │ Executor │ │  │ │ Executor │ │          │
│  │ └──────────┘ │  │ └──────────┘ │  │ └──────────┘ │          │
│  └──────────────┘  └──────────────┘  └──────────────┘          │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Background Services                         │   │
│  │  • Monitoring Pruner    • Digest Summarizer (Layer 3)   │   │
│  │  • Loop Detector (L5)   • Decision Rollup               │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PERSISTENCE LAYER                             │
│                                                                 │
│  monitoring.db (shared)     Per-agent queue DBs    Browser      │
│  • events table             • tasks table          profiles     │
│  • messages table           (WAL mode)             (per-team)   │
│  • decisions table                                              │
│  • delegations table        agents_config.json                  │
│  • actions table            (team/agent config)                 │
│  • digests table                                                │
│  • milestones table         Per-agent .hermes/                  │
│  (all WAL mode)             (isolated HERMES_HOME)              │
└─────────────────────────────────────────────────────────────────┘
```

**Key design principles:**
- **In-process P2P**: agents communicate via an in-process registry (no network hops)
- **Per-agent isolation**: each agent has its own thread, HERMES_HOME, and queue DB
- **Event-driven**: agents wake immediately on new tasks (not fixed polling)
- **Crash-resilient**: WAL-mode SQLite, task recovery on restart, retry + dead-letter

---

## 2. Package Structure

```
swarm_server/
├── __init__.py          # Package version
├── __main__.py          # `python -m swarm_server` entry
├── cli.py               # `hermes-swarm up|init|doctor` CLI
├── server.py            # FastAPI app, REST routes, WebSocket, lifespan
├── agent.py             # AgentDaemon — the core agent wrapper (1600 lines)
├── config.py            # All configuration, env vars, config file I/O
├── queue.py             # SQLite-backed per-agent task queue
├── tools.py             # Custom tool schemas + handlers (10 tools)
├── prompts.py           # All prompt templates + context composers
├── monitoring.py        # MonitoringDB — events, messages, decisions, actions
├── websocket.py         # WSBroadcaster + thread-safe _broadcast()
├── browser_pool.py      # Per-team persistent Chrome management
├── model_config.py      # Multi-provider model resolution
├── summarizer.py        # Layer 3: digest summarizer + decision rollup
├── loop_detector.py     # Layer 5: cross-agent loop/stall detector
├── cron.py              # Dependency-free cron parser + scheduler
└── web_crawl4ai.py      # Web crawling utilities
```

---

## 3. Startup & Lifecycle

### Entry Points

```
hermes-swarm up          # CLI (recommended)
python -m swarm_server   # Module entry
docker-compose up        # Docker
```

All paths reach `uvicorn.run("swarm_server.server:app", ...)`.

### Lifespan (server.py)

```python
@asynccontextmanager
async def lifespan(app):
    # STARTUP
    1. Capture the main asyncio event loop (for thread-safe broadcasts)
    2. Load agents_config.json
    3. For each agent: create AgentDaemon → recover stranded tasks → start sweep loop
    4. Start background tasks:
       - _periodic_monitoring_prune()   # bounded DB growth
       - _periodic_digest()             # Layer 3 summaries
       - _periodic_loop_detector()      # Layer 5 loop detection

    yield  # server running

    # SHUTDOWN
    1. Cancel background tasks
    2. Stop each daemon (cancel sweep, shutdown executor)
    3. Shutdown all team browsers (profiles persist on disk)
```

**Critical:** Queue DBs are NOT deleted on startup. Each `AgentDaemon` calls `queue.recover_processing()` to requeue any tasks stranded by a previous crash.

---

## 4. Agent Daemon

`AgentDaemon` (agent.py) is the core abstraction — one instance per agent. It wraps a Hermes `AIAgent` with a task queue, sweep loop, and lifecycle management.

### State Machine

```
                  ┌──────────┐
         ┌───────│  IDLE     │◄──────────────────┐
         │       └────┬─────┘                    │
         │            │ task arrives              │ batch complete
         │            ▼                           │
         │       ┌──────────┐                    │
         │       │  BUSY     │────────────────────┘
         │       └────┬─────┘
         │            │ ask_human tool called
         │            ▼
         │       ┌──────────────┐
         │       │ ASKING_HUMAN │──── human responds ──► BUSY
         │       └──────────────┘
         │
         │  pause (supervisor/human)
         ▼
    ┌──────────┐
    │  PAUSED  │──── resume ──► IDLE (queue preserved)
    └──────────┘
```

### Thread Isolation

Each agent gets its own `ThreadPoolExecutor(max_workers=1)`:

```python
self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"agent-{name}")
```

This ensures:
- A blocking `ask_human` wait stalls only THIS agent, not the entire swarm
- Each agent's `ContextVar` HERMES_HOME override is thread-local
- Agents serialize their own turns (no concurrent LLM calls per agent)

### Sweep Loop

The sweep loop is event-driven, not fixed-interval:

```python
async def sweep_loop(self):
    while True:
        # Wait for: new task signal OR periodic timeout OR cron due
        timeout = min(self._sweep_interval, self._next_cron_due_in())
        await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        self._wake.clear()

        await self._sweep()           # drain queue, process batch
        self._maybe_fire_crons()      # scheduled wake-ups
        self._maybe_autonomous_heartbeat()  # 24/7 self-drive
        self._maybe_feed_supervisor()  # Layer 4 peer review
```

When a task is ingested, `_signal_wake()` sets the `asyncio.Event` immediately, so the agent processes within milliseconds (not the 10s poll interval).

### Task Processing Flow

```
1. drain_pending(limit=MAX_BATCH_SIZE) → claim tasks as 'processing'
2. Combine tasks into one prompt: "You have N new message(s)..."
3. Compose live context (project tree, recent messages, decisions)
4. Prepend live context to the user turn (NOT the system prompt — cache-friendly)
5. Run on the agent's dedicated worker thread:
   a. Set thread-local HERMES_HOME override
   b. Ensure team browser is healthy
   c. Call AIAgent.run_conversation()
   d. Reset HERMES_HOME override
6. Handle result:
   - Success → log messages, broadcast, mark tasks done
   - LLM failure (infra) → requeue_no_penalty (retry on next sweep)
   - LLM failure (other) → requeue with retry counter → dead-letter after MAX_TASK_RETRIES
   - Exception → requeue_or_deadletter
7. Collect telemetry (tokens, context usage, cost)
```

---

## 5. Task Queue

`TaskQueue` (queue.py) — one SQLite DB per agent.

### Schema

```sql
CREATE TABLE tasks (
    id           TEXT PRIMARY KEY,
    from_agent   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending|processing|done|failed
    created_at   REAL NOT NULL,
    processed_at REAL,
    retries      INTEGER NOT NULL DEFAULT 0
);
```

### Key Operations

| Method | Behavior |
|--------|----------|
| `enqueue()` | Insert a new pending task |
| `drain_pending(limit)` | Atomically claim up to N pending tasks → 'processing' |
| `mark_done(id)` | Mark task completed |
| `requeue(ids)` | Move back to 'pending', increment retry counter |
| `requeue_no_penalty(ids)` | Move back to 'pending' WITHOUT incrementing retries (infra failures) |
| `mark_failed(ids)` | Dead-letter (exhausted retries) |
| `recover_processing()` | On startup: requeue all 'processing' tasks (crash recovery) |

### Durability Settings

```python
PRAGMA journal_mode=WAL;      # concurrent readers + writer
PRAGMA busy_timeout=10000;     # wait 10s on contention instead of erroring
PRAGMA synchronous=NORMAL;     # fsync on checkpoint, not every commit
```

---

## 6. Communication — Peer Messaging

Agents communicate via `send_peer_message`, which writes directly to the target agent's SQLite queue — no HTTP, no network.

```
Agent A calls send_peer_message(to_agent="B", message="...", kind="TASK")
    │
    ▼
_send_peer_message_handler() looks up B in _daemon_registry (in-process dict)
    │
    ▼
B.ingest_task(from_agent="A", payload="...") → writes to B's SQLite queue
    │
    ▼
B._signal_wake() → sets B's asyncio.Event → B wakes immediately
    │
    ▼
B's next sweep drains the task and processes it
```

### Message Types (kind)

| Kind | Wakes Recipient? | Purpose |
|------|:-:|---------|
| `TASK` | ✅ | Delegate work — recipient owes a RESULT |
| `QUESTION` | ✅ | Ask something needed to proceed |
| `RESULT` | ✅ | Report finished work, closes the delegation |
| `STATUS` | ❌ | Progress update, appears in feed only |
| `FYI` | ❌ | Informational note |

Non-waking messages (`STATUS`/`FYI`) are recorded in monitoring.db but do NOT inject into the recipient's queue, preventing acknowledgement loops.

### Delegation Ledger

Every `TASK`/`QUESTION` opens a row in `monitoring.db → delegations`. A matching `RESULT` with `reply_to=<task_id>` closes it. This gives the system a live view of outstanding work without polling.

---

## 7. Prompt Architecture

The prompt is split into layers for cache efficiency:

```
┌─────────────────────────────────────────────┐
│  SOUL.md (HERMES_HOME/SOUL.md)              │  ◄─ Written once at agent init
│  Agent identity + role description           │     Cache-stable (Hermes loads this)
├─────────────────────────────────────────────┤
│  Ephemeral System Prompt (base)              │  ◄─ Set once, stable across turns
│  COMMON_SOUL_TEMPLATE: swarm rules, tools,  │     Prefix-cache friendly
│  message protocol, memory architecture,      │
│  team org (peer roster only, not full team)  │
├─────────────────────────────────────────────┤
│  Conversation History                        │  ◄─ From Hermes SessionDB
│  (auto-compacted by Hermes)                  │     Grows, then compacts
├─────────────────────────────────────────────┤
│  LIVE CONTEXT (prepended to user turn)       │  ◄─ Rebuilt EVERY turn (volatile)
│  • Current time                              │     Injected into user message,
│  • workspace.md (project brief)              │     NOT system prompt (preserves
│  • Project directory tree                    │     prefix cache on system+history)
│  • Decision log (last 20)                    │
│  • Outstanding delegations                   │
│  • Recent peer messages (last 10)            │
│  • Cron schedule summary                     │
├─────────────────────────────────────────────┤
│  User Turn (the actual task batch)           │
│  "You have N new message(s) to process:..." │
└─────────────────────────────────────────────┘
```

**Key design decision:** Live context goes in the USER turn, not the system prompt. The system prompt (SOUL + common template) is cache-stable — mutating it every turn would break the LLM provider's prefix cache and re-bill the entire history as uncached input.

---

## 8. Tool System

### Swarm-Native Tools (10 total)

| Tool | Purpose | Scope |
|------|---------|-------|
| `send_peer_message` | P2P messaging (required, cannot be disabled) | All agents |
| `ask_human` | Block until human answers | All agents |
| `request_human_takeover` | Hand browser to human for login/CAPTCHA | All agents |
| `log_decision` | Append to shared team decision log | All agents |
| `log_action` | Idempotency-keyed action audit trail | All agents |
| `get_self_config` | Read own config + runtime telemetry | All agents |
| `request_config_change` | Propose config change (human approves) | All agents |
| `schedule_wakeup` | Create a cron wake-up | All agents |
| `cancel_wakeup` | Remove a cron wake-up | All agents |
| `pause_agent` / `resume_agent` | Emergency brake (supervisor-only) | Supervisors |

### Hermes Toolsets

Each agent also inherits Hermes' built-in toolsets (file, terminal, web_search, browser, etc.). The swarm controls which toolsets are enabled/disabled per agent via `enabled_toolsets` / `disabled_toolsets` in the agent config.

Unnecessary toolsets (TTS, image generation, etc.) are stripped to save tokens and reduce attack surface.

### Tool Registration Flow

```
1. AgentDaemon._ensure_agent() creates the Hermes AIAgent
2. _register_custom_tools() registers all swarm tools in Hermes' global registry
3. Swarm tool schemas are injected into this agent's tool list
4. Disabled tools are filtered out (except send_peer_message — always required)
5. Supervisor-only tools (pause/resume) added only for is_supervisor agents
```

---

## 9. Observability Stack (5 Layers)

```
Layer 1: Event Log          Raw events + messages → monitoring.db
Layer 2: Dashboard          Real-time WebSocket stream → browser UI
Layer 3: Digest Summarizer  LLM-powered rolling status per agent
Layer 4: Supervisor Agent   Peer review of linked agents' transcripts
Layer 5: Loop Detector      Cross-agent loop/stall detection + auto-intervention
```

### Layer 1 — Event Log (monitoring.py)

All agent activity is recorded in a shared `monitoring.db`:

- **events**: state changes, task lifecycle, errors, token usage, cron fires
- **messages**: full conversation transcripts (user/assistant/tool/system)
- **decisions**: team decision log (shared memory)
- **delegations**: TASK/QUESTION → RESULT correlation ledger
- **actions**: idempotency-keyed audit trail of external side effects
- **digests**: rolling status summaries per agent
- **milestones**: rolled-up long-term decision memory

Periodic pruning keeps the DB bounded during 24/7 operation.

### Layer 2 — Dashboard (websocket.py)

`WSBroadcaster` pushes every event to connected dashboard clients via WebSocket. Thread-safe: agent worker threads call `_broadcast()` which hops to the main event loop via `run_coroutine_threadsafe()`. Per-client send timeout (5s) prevents a slow client from stalling delivery to others.

### Layer 3 — Digest Summarizer (summarizer.py)

A background loop runs every `DIGEST_SWEEP_INTERVAL_SECONDS`:

```
For each agent:
  1. Check: new messages since last digest?
  2. Hybrid trigger: enough new tokens OR enough time elapsed?
  3. Build transcript from new messages (capped, rolling)
  4. LLM call (cheap model) → structured JSON:
     {headline, did[], blocked_on, next, risk_level}
  5. Store digest in monitoring.db, broadcast to dashboard
```

Risk levels: `ok` → `watch` → `stuck` → `error`

Also runs **decision rollup**: when decisions scroll past the live injection window, they're summarized into a "milestone" so long-term team memory isn't lost.

### Layer 4 — Supervisor Agents (agent.py)

Agents with `is_supervisor: true` automatically receive linked peers' transcripts:

```
1. _maybe_feed_supervisor() runs each sweep tick
2. For each linked peer: check new activity since last review (watermark)
3. Pick the peer with the MOST unreviewed tokens (prevents starvation)
4. Render transcript + compute progress signal:
   - Count concrete external actions vs. near-duplicate re-confirmations
   - Flag NO-PROGRESS LOOPs deterministically (code, not model judgment)
5. Inject as a task: "[SUPERVISOR REVIEW — '{peer}' produced ~N new tokens...]"
6. Advance watermark
```

The progress signal is computed in code so the supervisor judges real progress, not token volume.

### Layer 5 — Loop Detector (loop_detector.py)

Watches the team's message graph as a whole (cross-agent, unlike Layer 4):

**Detector A — Pair Ping-Pong:**
- Find A↔B pairs trading many WAKING messages with repetitive content
- Threshold: `LOOP_PAIR_THRESHOLD` messages + >40% duplicate content

**Detector B — Team Stall:**
- Many messages in window + ZERO decisions + ZERO actions logged
- Threshold: `LOOP_TEAM_MSG_THRESHOLD` messages

On detection: inject a corrective "[SYSTEM — LOOP BREAKER]" task to the busiest involved agent + record a team-visible decision log note. Cooldown dedup prevents re-alerting.

---

## 10. Browser Pool

`TeamBrowserManager` (browser_pool.py) — one persistent Chrome per team.

### Two-Mode Design

| Mode | When | How |
|------|------|-----|
| **Headless** | Normal agent work | `--headless=new`, clean user-agent, CDP screenshots work |
| **Headful** | Human login (takeover) | Real visible window on human's screen, same profile |

### Key Features

- **Shared session**: all agents in a team share one browser → same cookies/logins
- **Persistent profile**: `data/teams/<team>/.browser-profile/` survives restarts
- **Graceful relaunch**: CDP `Browser.close` flushes cookies before mode switch
- **Browser discovery**: prefers installed Chrome/Edge over Playwright Chromium (real branding passes sign-in checks better)
- **Clean UA**: headless mode strips `HeadlessChrome` from User-Agent

### Takeover Flow

```
1. Agent hits login wall → calls request_human_takeover
2. begin_takeover(): capture current URL, kill headless, relaunch headful on same profile
3. Human sees real Chrome window on their screen, logs in
4. Human responds in dashboard
5. end_takeover(): kill headful, relaunch headless → agent resumes authenticated
```

---

## 11. Model Configuration

`model_config.py` — resolution precedence:

```
Per-agent override → Swarm default → Legacy LiteLLM proxy fallback
```

- **Swarm default**: stored in Hermes config format at `data/.hermes-shared/`
- **Per-agent override**: `model`, `provider`, `base_url`, `api_key` fields in agent config
- **Provider catalogue**: built from Hermes' `PROVIDER_REGISTRY` + OpenRouter + Custom
- **OpenAI-compatible providers** route via `provider=custom` + `base_url`
- **Native providers** use Hermes' built-in adapter

---

## 12. Cron Scheduler

`cron.py` — dependency-free, supports:

- **Standard 5-field**: `0 9 * * 1-5` (9am weekdays)
- **Macros**: `@hourly`, `@daily`, `@weekly`, `@monthly`, `@yearly`
- **Intervals**: `@every 30m`, `@every 2h`
- **Ranges/steps**: `1-5`, `*/15`, `1-30/2`

Agents manage crons via `schedule_wakeup` / `cancel_wakeup` tools. When a cron fires, the `_maybe_fire_crons()` method injects the instruction as a task with a `[SCHEDULED WAKE-UP]` wrapper.

Cron next-fire times survive config reloads — only schedules that actually changed are recomputed.

---

## 13. Security

### Authentication

```python
@app.middleware("http")
async def _auth_guard(request, call_next):
    if SWARM_API_KEY and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # Check Authorization: Bearer or X-API-Key header
```

- **Off by default** (server binds localhost)
- **Activated** by setting `SWARM_API_KEY` env var
- **Protects** all mutating endpoints; GET + dashboard remain open
- **For internet exposure**: must place behind TLS reverse proxy

### CORS

Restricted to configured origins (defaults to `localhost:PORT`). Override with `SWARM_CORS_ORIGINS` env var.

### Agent Isolation

- Each agent has its own `HERMES_HOME` (ContextVar-scoped, not env var)
- Agents can only message peers in their `allowed_peers` list
- Tool access controlled per-agent via `enabled_toolsets` / `disabled_toolsets`
- Self-config changes require human approval (proposal → approve/reject flow)

---

## 14. Persistence & Data Layout

```
$SWARM_DATA_DIR/                        # default: ./data or ~/.hermes-swarm/data
├── agents_config.json                  # All teams + agents config
├── monitoring.db                       # Shared monitoring database
├── .hermes-shared/                     # Swarm-wide default model config
│   ├── config.yaml
│   └── .env
└── teams/
    └── <team_id>/
        ├── workspace.md                # Shared project brief
        ├── project/                    # Shared working directory (code, files)
        ├── .browser-profile/           # Persistent Chrome profile
        └── workspace/
            └── <agent_name>/
                ├── .hermes/            # Agent's isolated Hermes home
                │   ├── config.yaml     # Agent-specific Hermes config
                │   ├── SOUL.md         # Agent identity
                │   └── sessions/       # Conversation history DB
                └── <agent>_queue.db    # Agent's task queue
```

---

## 15. WebSocket Real-Time Layer

### Event Types Broadcast

| Event | When |
|-------|------|
| `state_change` | Agent state transitions (idle/busy/paused/asking_human) |
| `queue_updated` | Task enqueued or drained |
| `task_dequeued` / `task_failed` | Task lifecycle |
| `conversation_start` / `conversation_complete` | LLM turn boundaries |
| `message_logged` | New message recorded to monitoring.db |
| `agent_exec` | Live execution trace (thinking/tool_start/tool_result/token) |
| `telemetry` | Token usage, context occupancy, cost |
| `digest_updated` | New Layer 3 digest available |
| `loop_detected` | Layer 5 loop/stall alert |
| `cron_fired` / `cron_updated` | Cron lifecycle |
| `human_responded` | Human answered a question |
| `proposal_resolved` | Config change approved/rejected |
| `error` | Error in agent processing |

### Thread Safety

Agent worker threads call `_broadcast()`, which detects whether it's on the event loop thread or a worker thread and routes accordingly:

```python
def _broadcast(event_type, payload):
    try:
        current_loop = asyncio.get_running_loop()
        if current_loop is _main_event_loop:
            asyncio.create_task(ws_broadcaster.broadcast(...))  # same thread
    except RuntimeError:
        asyncio.run_coroutine_threadsafe(...)  # worker thread → hop to event loop
```

---

## 16. Deployment

### Docker (Recommended)

```yaml
# docker-compose.yml
services:
  swarm:
    build: .
    ports: ["127.0.0.1:8000:8000"]   # localhost only by default
    volumes: ["./data:/app/data"]      # persistent state
    environment:
      SWARM_HOST: "0.0.0.0"           # bind all interfaces inside container
      SWARM_LLM_BASE_URL: "..."       # your LLM endpoint
      SWARM_LLM_API_KEY: "..."
      SWARM_API_KEY: "..."            # required if exposing beyond localhost
```

### pip Install

```bash
pip install hermes-swarm
hermes-swarm init                     # scaffold starter team
hermes-swarm doctor                   # preflight check
hermes-swarm up                       # launch
```

### Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SWARM_HOST` | `127.0.0.1` | Bind address |
| `SWARM_PORT` | `8000` | Server port |
| `SWARM_LLM_BASE_URL` | `http://127.0.0.1:4000/v1` | LLM endpoint |
| `SWARM_LLM_API_KEY` | `sk-1234` | LLM API key |
| `SWARM_API_KEY` | (empty) | Auth token for mutating endpoints |
| `SWARM_DATA_DIR` | `./data` | Persistent state directory |
| `SWARM_CORS_ORIGINS` | `localhost` | Allowed CORS origins |
| `SWARM_SWEEP_INTERVAL` | `10` | Default sweep interval (seconds) |
| `SWARM_DEFAULT_MODEL` | `litellm-model` | Default LLM model |
| `SWARM_ASK_HUMAN_WAIT_SECONDS` | `21600` | Human-wait timeout (6 hours) |
