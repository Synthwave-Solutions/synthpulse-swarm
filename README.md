# Hermes Swarm

A self-hostable **multi-agent swarm server** with a real-time dashboard. Each
agent is a full [Hermes](https://github.com/NousResearch/hermes-agent) agent —
it can browse the web, run a terminal, write files, and publish to live
platforms — and agents collaborate peer-to-peer on a shared project. You watch
and steer the whole team from one web UI.

- **Live execution view** — see each agent think → call tools → answer, in real time.
- **Per-agent config** — model, provider, tools, reasoning, iterations, sampling — from the UI.
- **Self-aware agents** — agents can read their own config/telemetry and *propose* changes for your approval.
- **Human inbox** — agents ask you for logins/decisions; you reply from the dashboard.

> **New here?** The **[Getting Started guide](docs/getting-started.md)** walks
> you from download through building and running your first team. Exposing it on
> a server? See **[Deploying on a VPS](docs/deploy-vps.md)**.

---

## Prerequisites

1. **An LLM provider API key.** You configure it with **`hermes setup`** — Hermes'
   own one-command wizard that lets you pick from 40+ providers (Anthropic,
   OpenAI, OpenRouter, Groq, DeepSeek, a local model, …), paste your key, and
   choose a model. The swarm reads that config directly; there's nothing
   provider-specific to set up separately. *(Using a
   [LiteLLM](https://github.com/BerriAI/litellm) proxy or any OpenAI-compatible
   endpoint? It's just another provider — pick **custom** in `hermes setup` and
   enter its base URL + key. No swarm-specific config.)*
2. **Docker** *(option A)* **or Python 3.11+** *(option B)*.

You do **not** need to install Hermes separately — it comes in automatically,
and the `hermes` CLI ships with it (see [Already have Hermes?](#already-have-hermes)).

---

## Install

Each path ends with the dashboard on **http://127.0.0.1:8000**.

### One line — macOS & Linux

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/CyberTron957/hermes-mission-control/main/install.sh)
```

That clones the repo, sets up a local `.venv`, installs everything, fetches a
browser, runs `hermes setup` (40+ providers, incl. a custom / OpenAI-compatible
endpoint), scaffolds a starter team, verifies with `hermes-swarm doctor`, and
offers to start the dashboard. Already have `~/.hermes`? It adopts your provider
+ keys and skips setup. Safe to re-run. *(Flags after the URL, e.g.
`… ) --no-run`; or `curl -fsSL … | bash`, which installs then prints the
commands to configure + start.)*

**Windows:** run that line in [WSL](https://learn.microsoft.com/windows/wsl/),
or use Docker below.

**Already cloned the repo?** `bash install.sh` from inside it does the same thing.

### Docker

```bash
git clone <this-repo> hermes-swarm && cd hermes-swarm
docker compose run --rm -e HERMES_HOME=/data/.hermes-shared swarm hermes setup
docker compose up --build
```

The middle step picks your provider (written to the volume's shared config); the
image bundles Python, Hermes, Chromium, and the dashboard, with data persisted in
the `swarm-data` volume. Using a LiteLLM proxy or other OpenAI-compatible
endpoint? Pick **custom** in `hermes setup` and enter its base URL + key (for one
on your host, use `http://host.docker.internal:<port>`). To require an API key
before exposing the port, `cp .env.example .env` and set `SWARM_API_KEY`.

---

## Already have Hermes?

If you already use Hermes (`~/.hermes/`), there's nothing extra to do:

- **Runtime** — the swarm reuses your installed agent runtime. Resolution order:
  the pip-installed `hermes-agent`, else `HERMES_AGENT_PATH`, else
  `~/.hermes/hermes-agent`.
- **Provider/model** — if you've already run `hermes setup`, the swarm **adopts
  that config automatically** (you don't run it again). It *reads* your provider
  + model + key from `~/.hermes`; it never writes there.
- **Secrets** — all of your `~/.hermes/.env` keys are imported into the swarm at
  startup (not just the model provider's key) — so tool keys like
  `FIRECRAWL_API_KEY`, `TAVILY_API_KEY`, `EXA_API_KEY`, etc. are available to
  agents too. Import is non-overriding: explicit server/deployment env wins.
- **Isolation** — your personal `~/.hermes` is left untouched. Each agent runs in
  its own private Hermes home under `<data>/teams/…`, so swarm agents don't share
  (or pollute) your memory, sessions, or SOUL.md. Picking a default model in the
  dashboard writes to the swarm's *own* shared config (`<data>/.hermes-shared`),
  not `~/.hermes`.

Precedence is: per-agent override → swarm default → your `~/.hermes`. Run
`hermes-swarm doctor` to see exactly which one is in effect.

---

## Configuration (environment variables)

> **Provider & model are configured with `hermes setup`, not env vars** — that
> includes custom / OpenAI-compatible endpoints (pick the **custom** provider).
> The swarm has no proxy of its own. Per-agent overrides (any provider/model) are
> set live from the dashboard.

| Variable | Default | Purpose |
|---|---|---|
| `SWARM_DEFAULT_MODEL` | *(unset)* | optional: force a default model name without the wizard |
| `SWARM_FALLBACK_MODELS` | *(unset)* | optional: model-dropdown list when the backend exposes no `/models` |
| `SWARM_VISION_MODEL` | *(unset)* | optional: separate multimodal model, only for a text-only custom endpoint |
| `SWARM_HOST` / `SWARM_PORT` | `127.0.0.1` / `8000` | dashboard bind address |
| `SWARM_API_KEY` | *(unset)* | if set, required on **every** endpoint + WebSocket — the dashboard prompts for it once and remembers it. **Set it whenever you expose the port.** |
| `SWARM_DATA_DIR` | repo `./data` or `~/.hermes-swarm/data` | writable state (configs, queues, workspaces) |
| `HERMES_AGENT_PATH` | *(unset)* | path to a Hermes source checkout (only if not pip-installed) |

Per-agent settings (model, provider, tools, reasoning, iterations, sampling,
soul, autonomous wake-up interval, and **cron wake-ups**) are edited live from
the dashboard's ⚙️ panel — no restart needed. `SWARM_HEARTBEAT_SECONDS` is just
the default; each agent's idle wake-up cadence can be overridden in its settings.

**Scheduled wake-ups (cron):** every agent can run recurring work on a schedule
— a 9am competitor check, an hourly metrics pull, a Monday digest. Add/enable/
delete schedules from the ⚙️ panel (5-field cron, `@hourly`/`@daily`/`@weekly`/
`@monthly`, or intervals like `@every 30m`); agents can also self-schedule and
cancel their own via the `schedule_wakeup` / `cancel_wakeup` tools.

---

## Notes

- **Security:** by default the server binds `127.0.0.1` with no key. To expose
  it (VPS/LAN), set `SWARM_API_KEY` — it then guards every HTTP endpoint *and*
  the WebSocket, and the dashboard prompts for the key once (stored in your
  browser). Put it behind a TLS reverse proxy too. Agents can run terminal
  commands as the server user, so on a shared/exposed host prefer the Docker
  route. See [`docs/deploy-vps.md`](docs/deploy-vps.md) for a hardened setup.
- **Chromium** is required only for the browser-publishing tools; everything
  else works without it (the swarm degrades gracefully).
- **Web research:** agents get `web_search` + `web_extract`. Search works with no
  setup (DuckDuckGo). For *extract*, if you've configured a Hermes web backend
  (Firecrawl/Tavily/Exa/… via `hermes tools` or API-key env vars) the swarm uses
  it untouched; otherwise it falls back to a built-in fetcher — `httpx` out of
  the box (fine for normal pages), or install `pip install .[web]` (crawl4ai +
  `playwright install chromium`) for JavaScript-heavy single-page apps.
- **Data & backups:** state lives in `SWARM_DATA_DIR`; every config save keeps a
  rotating backup under `<data>/config_backups/`.
