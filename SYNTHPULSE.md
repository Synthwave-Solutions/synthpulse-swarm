# SynthPulse Swarm

This repository is **SynthPulse Swarm**, Synthwave Solutions' productised, branded
fork of [Hermes Swarm](https://github.com/CyberTron957/hermes-swarm) (MIT) by
Pradhyun: a self-hosted server that runs teams of full
[Hermes](https://github.com/NousResearch/hermes-agent) agents 24/7, with a
real-time mission-control dashboard.

It is part of the **SynthPulse Agentic Workstation** stack and ships as the
`synthpulse-swarm` service alongside our branded Hermes core, OpenDesign,
OpenCode, the LiteLLM gateway, and Langfuse audit.

## What we changed

The fork is a **non-invasive rebrand**: we keep the engine and wire layer
upstream-compatible so we can `git merge upstream/main` cleanly, and we only
overlay the SynthPulse identity.

- **Dashboard** (`dashboard/index.html`): Deep Navy canvas + Wave Blue (`#009dff`)
  accent design tokens, Montserrat typography, the SynthPulse wavemark logo, and
  "SynthPulse Swarm" naming.
- **Landing page** (`landing/index.html`): full SynthPulse re-skin (same tokens),
  wavemark, repo links pointing at this fork.
- **Brand assets**: wavemark favicon.
- **Packaging / server name**: `pyproject` name `synthpulse-swarm`, server banners
  and the FastAPI title read "SynthPulse Swarm Server".

References to **Hermes** are kept where they are accurate technical or engine
references (the `hermes` CLI, Hermes config format, "each agent is a full Hermes
agent", the `NousResearch/hermes-agent` engine). The product name is SynthPulse
Swarm; the engine is Hermes.

## Brand tokens

| Token | Value |
|---|---|
| Deep Navy (surfaces) | `#0f172a` / `#1e293b` / `#070e18` |
| Wave Blue (accent) | `#009dff` (bright `#33b3ff`) |
| Brand cyan (secondary) | `#48d9fe` |
| On-navy text | `#f8fafc` (muted `#a3b1c6`) |
| Headings font | Montserrat |
| Body font | Roboto / Montserrat |
| Mark | `brand/wavemark.svg` (orbit ring + Wave Blue pulse) |

## Upstream

- `upstream` remote = `CyberTron957/hermes-swarm` (MIT). Pull updates, then
  re-apply or rebase the branding overlay. Verify zero em or en dashes before any
  push: `grep -rlP '[\x{2014}\x{2013}]' .`
- Commits and tags go as Michael Ramirez only, no co-author trailer.

License: MIT (unchanged). See `LICENSE`.
