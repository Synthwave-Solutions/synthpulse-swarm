"""Configuration constants and agent config management."""

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("swarm.config")

# ---------------------------------------------------------------------------
# Paths (relative to project root)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"
AGENTS_CONFIG_PATH = DATA_ROOT / "agents_config.json"
MONITORING_DB = DATA_ROOT / "monitoring.db"
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"

WORKSPACE_ROOT = DATA_ROOT / "teams"

# ---------------------------------------------------------------------------
# Network / Runtime
# ---------------------------------------------------------------------------
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
LITELLM_API_BASE = f"http://{SERVER_HOST}:4000/v1"
SWEEP_INTERVAL_SECONDS = 10

# ---------------------------------------------------------------------------
# Common SOUL — shared by all agents in the framework
# Injected at runtime with: {agent_name}, {team_id}, {allowed_peers_list}
# ---------------------------------------------------------------------------
COMMON_SOUL_TEMPLATE = (
    "You are an autonomous agent in a multi-agent swarm.\n"
    "You operate within an async system: tasks arrive in batches and are processed "
    "every ~{sweep_interval}s. Responses are not immediate.\n\n"
    "--- SWARM RULES ---\n"
    "1. NEVER end your turn silently. Always conclude by calling a tool.\n"
    "   - Use 'send_peer_message' to send results, delegate tasks, or chat with peers.\n"
    "   - Use 'ask_human' only when genuinely stuck, instructions are ambiguous, "
    "     or a task requires human judgment (approvals, subjective choices).\n"
    "2. After calling 'send_peer_message' or 'ask_human', STOP calling tools and end your turn.\n"
    "3. Process tasks autonomously without asking for permission.\n"
    "4. Report completions, blockers, or delegation decisions back to the sender.\n"
    "5. Keep responses concise — other agents read them in batch.\n\n"
    "--- COMMUNICATION PROTOCOL ---\n"
    "Use these formats when messaging peers:\n"
    "  TASK: [description] | DELEGATE TO: [peer] — assign a task to a peer\n"
    "  STATUS: [what you did] | BLOCKERS: [any] — progress update\n"
    "  RESULT: [output] | NEXT: [recommended action] — deliverable complete\n"
    "  HELP: [what you need] | CONTEXT: [background] — request assistance\n\n"
    "--- PEERS ---\n"
    "Your agent name: {agent_name}\n"
    "Your team: {team_id}\n"
    "Peers you are linked to: {allowed_peers_list}\n"
    "You may ONLY send messages to agents in your linked peers list.\n\n"
    "--- TOOL GUIDANCE ---\n"
    "You have access to the full Hermes tool suite (web search, terminal, file ops, "
    "browser, todo, memory, code execution, etc.). Use tools relevant to the task.\n"
    "You do NOT need permission to:\n"
    "  - Read/write files in your workspace\n"
    "  - Run terminal commands in your workspace\n"
    "  - Search the web\n"
    "  - Delegate to linked peers\n"
    "You MUST ask a human for approval before:\n"
    "  - Actions affecting systems outside your workspace\n"
    "  - Irreversible operations (deleting critical files, pushing code, spending resources)\n"
)


# ---------------------------------------------------------------------------
# New config schema helpers
# ---------------------------------------------------------------------------
def _derive_workspace_path(team_id: str, agent_name: str) -> Path:
    """Return the on-disk workspace directory for an agent."""
    return WORKSPACE_ROOT / team_id / "workspace" / agent_name


def _migrate_legacy_config(legacy: Dict[str, Any]) -> Dict[str, Any]:
    """Convert old flat agent config format -> new {teams, agents} format."""
    log.info("Migrating legacy flat agent config -> new teams schema")
    default_team_id = "default"
    migrated = {
        "teams": {
            default_team_id: {"name": "Default Team", "created_at": 0},
        },
        "agents": {},
    }
    for name, cfg in legacy.items():
        old_ws = cfg.get("workspace", name)
        new_path = _derive_workspace_path(default_team_id, name)
        old_path = DATA_ROOT / old_ws

        # Move existing disk data into new path
        if old_path.exists() and old_path != new_path:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            if new_path.exists():
                shutil.rmtree(new_path)
            shutil.move(str(old_path), str(new_path))
            log.info("  Moved %s -> %s", old_path, new_path)

        # v2 migration: extract role-specific part from old monolithic soul
        old_soul = cfg.get("soul", "")
        # If old soul looks like the old default template, extract just the identity line
        if "You are the" in old_soul and "send_peer_message" in old_soul:
            # Extract the first sentence as role identity
            lines = old_soul.split('\n')
            role_lines = []
            for line in lines:
                if line.startswith("You are the") or line.startswith("You are a"):
                    role_lines.append(line)
                elif role_lines and line.strip() and not any(x in line for x in ["send_peer_message", "ask_human", "operate autonomously"]):
                    role_lines.append(line)
            role_soul = '\n'.join(role_lines) if role_lines else old_soul.split('.')[0] + "."
        else:
            role_soul = old_soul

        migrated["agents"][name] = {
            "team_id": default_team_id,
            "name": cfg.get("name", name.capitalize() + " Agent"),
            "session_id": cfg.get("session_id", f"{name}-master-session-v1"),
            "allowed_peers": [],
            "role_soul": role_soul,
        }
    return migrated


def _migrate_v1_to_v2(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Migrate v1 config (has 'soul' field) to v2 config (has 'role_soul' field)."""
    migrated_any = False
    for name, agent_cfg in cfg.get("agents", {}).items():
        if "soul" in agent_cfg and "role_soul" not in agent_cfg:
            old_soul = agent_cfg.pop("soul")
            # Extract role-specific part from old monolithic soul
            lines = old_soul.split('\n')
            role_lines = []
            for line in lines:
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                if any(x in line_stripped for x in ["send_peer_message", "ask_human", "operate autonomously",
                                                      "swarm rules", "communication protocol", "tool guidance",
                                                      "You are an autonomous agent", "NEVER end your turn"]):
                    continue
                role_lines.append(line_stripped)
            agent_cfg["role_soul"] = '\n'.join(role_lines) if role_lines else f"You are the {agent_cfg.get('name', name)}."
            migrated_any = True
    if migrated_any:
        log.info("Migrated v1 config (soul field) -> v2 config (role_soul field)")
        _save_full_config(cfg)
    return cfg


def _deep_copy_config(src: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(json.dumps(src))


def _default_config() -> Dict[str, Any]:
    default_team_id = "default"
    return {
        "teams": {
            default_team_id: {"name": "Default Team", "created_at": int(time.time())},
        },
        "agents": {},
    }


# ---------------------------------------------------------------------------
# Soul composition — combines common + role-specific parts
# ---------------------------------------------------------------------------
def compose_agent_soul(agent_cfg: Dict[str, Any]) -> str:
    """Build the full ephemeral system prompt for an agent."""
    agent_name = agent_cfg.get("name", "Agent")
    team_id = agent_cfg.get("team_id", "default")
    peers = agent_cfg.get("allowed_peers", [])
    peers_str = ", ".join(peers) if peers else "(none — you cannot message any peers yet)"

    common = COMMON_SOUL_TEMPLATE.format(
        agent_name=agent_name,
        team_id=team_id,
        allowed_peers_list=peers_str,
        sweep_interval=SWEEP_INTERVAL_SECONDS,
    )

    role = agent_cfg.get("role_soul", f"You are the {agent_name}.")

    return (
        f"{common}\n"
        f"{'=' * 60}\n"
        f"YOUR ROLE\n"
        f"{'=' * 60}\n"
        f"{role}\n"
    )


# ---------------------------------------------------------------------------
# Config I/O
# ---------------------------------------------------------------------------
def load_agents_config() -> Dict[str, Any]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    if not AGENTS_CONFIG_PATH.exists():
        default_cfg = _default_config()
        with open(AGENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=4)
        return default_cfg

    try:
        with open(AGENTS_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log.error("Failed to load agents config: %s. Returning default.", e)
        return _default_config()

    # Detect legacy flat format (has string keys at top level with agent dict values)
    if "teams" not in raw and "agents" not in raw:
        migrated = _migrate_legacy_config(raw)
        _save_full_config(migrated)
        return migrated

    # Ensure both keys exist even if someone corrupted the file
    if "teams" not in raw:
        raw["teams"] = {}
    if "agents" not in raw:
        raw["agents"] = {}

    # Migrate v1 -> v2 if needed
    raw = _migrate_v1_to_v2(raw)
    return raw


def _save_full_config(cfg: Dict[str, Any]) -> None:
    with open(AGENTS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)


def save_agent_config(agent_name: str, cfg: Dict[str, Any]) -> None:
    full = load_agents_config()
    full["agents"][agent_name] = cfg
    _save_full_config(full)


def save_all_config(cfg: Dict[str, Any]) -> None:
    _save_full_config(cfg)


# ---------------------------------------------------------------------------
# Team CRUD
# ---------------------------------------------------------------------------
def create_team(cfg: Dict[str, Any], team_id: str, name: str) -> Dict[str, Any]:
    if team_id in cfg["teams"]:
        raise ValueError(f"Team '{team_id}' already exists")
    cfg["teams"][team_id] = {"name": name, "created_at": int(time.time())}
    # Ensure workspace directory exists
    (WORKSPACE_ROOT / team_id / "workspace").mkdir(parents=True, exist_ok=True)
    _save_full_config(cfg)
    return cfg["teams"][team_id]


def delete_team(cfg: Dict[str, Any], team_id: str) -> bool:
    if team_id not in cfg["teams"]:
        return False
    # Remove every agent in this team
    agents_to_remove = [
        name for name, a in cfg["agents"].items() if a.get("team_id") == team_id
    ]
    for name in agents_to_remove:
        del cfg["agents"][name]
    del cfg["teams"][team_id]
    # Nuke disk workspace
    team_dir = WORKSPACE_ROOT / team_id
    if team_dir.exists():
        shutil.rmtree(team_dir)
    _save_full_config(cfg)
    return True


def list_teams(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {"id": tid, "name": t["name"], "agent_count": sum(
            1 for a in cfg["agents"].values() if a.get("team_id") == tid
        )}
        for tid, t in cfg["teams"].items()
    ]


# ---------------------------------------------------------------------------
# Agent CRUD
# ---------------------------------------------------------------------------
def create_agent(
    cfg: Dict[str, Any],
    name: str,
    team_id: str,
    display_name: str,
    allowed_peers: Optional[List[str]] = None,
    role_soul: Optional[str] = None,
) -> Dict[str, Any]:
    if name in cfg["agents"]:
        raise ValueError(f"Agent '{name}' already exists")
    if team_id not in cfg["teams"]:
        raise ValueError(f"Team '{team_id}' does not exist")

    agent_cfg = {
        "team_id": team_id,
        "name": display_name,
        "session_id": f"{name}-master-session-v1",
        "allowed_peers": list(allowed_peers or []),
        "role_soul": role_soul or f"You are the {display_name}.",
    }
    cfg["agents"][name] = agent_cfg
    # Prepare workspace dirs
    ws = _derive_workspace_path(team_id, name)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "context").mkdir(exist_ok=True)
    _save_full_config(cfg)
    return agent_cfg


def delete_agent(cfg: Dict[str, Any], name: str) -> bool:
    if name not in cfg["agents"]:
        return False
    team_id = cfg["agents"][name].get("team_id", "default")
    del cfg["agents"][name]

    # Remove workspace data permanently
    ws = _derive_workspace_path(team_id, name)
    if ws.exists():
        shutil.rmtree(ws)

    # Also prune this agent from every other agent's allowed_peers list
    for a_cfg in cfg["agents"].values():
        if name in a_cfg.get("allowed_peers", []):
            a_cfg["allowed_peers"].remove(name)

    _save_full_config(cfg)
    return True


def set_agent_peers(cfg: Dict[str, Any], name: str, peers: List[str]) -> Dict[str, Any]:
    if name not in cfg["agents"]:
        raise ValueError(f"Agent '{name}' not found")
    cfg["agents"][name]["allowed_peers"] = list(peers)
    _save_full_config(cfg)
    return cfg["agents"][name]


def add_agent_peer(cfg: Dict[str, Any], name: str, peer: str) -> Dict[str, Any]:
    if name not in cfg["agents"]:
        raise ValueError(f"Agent '{name}' not found")
    peers = cfg["agents"][name].get("allowed_peers", [])
    if peer not in peers:
        peers.append(peer)
        cfg["agents"][name]["allowed_peers"] = peers
        _save_full_config(cfg)
    return cfg["agents"][name]


def remove_agent_peer(cfg: Dict[str, Any], name: str, peer: str) -> Dict[str, Any]:
    if name not in cfg["agents"]:
        raise ValueError(f"Agent '{name}' not found")
    peers = cfg["agents"][name].get("allowed_peers", [])
    if peer in peers:
        peers.remove(peer)
        cfg["agents"][name]["allowed_peers"] = peers
        _save_full_config(cfg)
    return cfg["agents"][name]


# ---------------------------------------------------------------------------
# Team isolation helpers
# ---------------------------------------------------------------------------
def get_agent_team(cfg: Dict[str, Any], name: str) -> Optional[str]:
    agent = cfg["agents"].get(name)
    return agent["team_id"] if agent else None


def get_team_agents(cfg: Dict[str, Any], team_id: str) -> Dict[str, Any]:
    return {name: a for name, a in cfg["agents"].items() if a.get("team_id") == team_id}


def peer_allowed(cfg: Dict[str, Any], caller: str, target: str) -> bool:
    """Return True if caller is explicitly linked to target AND same team."""
    caller_cfg = cfg["agents"].get(caller)
    target_cfg = cfg["agents"].get(target)
    if not caller_cfg or not target_cfg:
        return False
    if caller_cfg.get("team_id") != target_cfg.get("team_id"):
        return False
    return target in caller_cfg.get("allowed_peers", [])


# Initial load
AGENTS = load_agents_config()
