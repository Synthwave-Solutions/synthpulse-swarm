#!/usr/bin/env python3
"""Unit tests for the GUI-grade browser tools (swarm_server/browser_gui_tools.py)
and the configurable vision model. No LLM, no Hermes, no browser required —
the agent-browser dispatch layer is stubbed.

Covers:
  1. SCHEMAS        — well-formed, unique names, required ⊆ properties.
  2. CLI MAPPING    — each handler emits the exact agent-browser command/args
                      (keyboard type vs inserttext, mouse move/down/up
                      sequences, wait capping, upload preflight).
  3. SESSION KEY    — _ab routes through _last_session_key + _run_browser_command.
  4. SCREENSHOT     — lands in the calling agent's workspace, dims parsed.
  5. LOCATE         — VLM JSON parsed defensively; screenshot-pixel → CSS-pixel
                      coordinate scaling against the live viewport.
  6. VISION MODEL   — settings override layered over the env/code default and
                      baked into write_agent_hermes_config's aux.vision.

Run:  pytest tests/test_browser_gui.py -v
"""

import json
import struct
import sys
import types
import zlib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from swarm_server import browser_gui_tools as gui  # noqa: E402
from swarm_server.browser_gui_tools import (  # noqa: E402
    GUI_BROWSER_TOOL_SCHEMAS,
    _browser_click_xy_handler,
    _browser_dblclick_handler,
    _browser_drag_handler,
    _browser_hover_handler,
    _browser_keys_handler,
    _browser_locate_handler,
    _browser_screenshot_handler,
    _browser_scrollintoview_handler,
    _browser_upload_handler,
    _browser_wait_handler,
    _parse_locate_reply,
    _png_dimensions,
)

KW = {"task_id": "agent_name:tester"}


def _minimal_png(width: int, height: int) -> bytes:
    """A tiny but VALID PNG with the given IHDR dimensions."""
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture()
def ab_recorder(monkeypatch):
    """Stub gui._ab: record every (command, args); screenshot commands create a
    real PNG at the path argument so existence checks pass."""
    calls = []

    def fake_ab(task_id, command, args=None, timeout=None):
        args = list(args or [])
        calls.append((command, args))
        if command == "screenshot":
            path = Path(args[-1])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_minimal_png(200, 100))
            return {"success": True, "data": {"path": str(path)}}
        return {"success": True, "data": {}}

    monkeypatch.setattr(gui, "_ab", fake_ab)
    monkeypatch.setattr(gui, "_breadcrumb",
                        lambda task_id: {"url": "https://x.test/p", "title": "X"})
    return calls


# ---------------------------------------------------------------------------
# 1. Schemas
# ---------------------------------------------------------------------------

def test_schemas_well_formed():
    names = []
    for schema in GUI_BROWSER_TOOL_SCHEMAS:
        assert schema["type"] == "function"
        fn = schema["function"]
        names.append(fn["name"])
        params = fn["parameters"]
        assert params["type"] == "object"
        for req in params.get("required", []):
            assert req in params["properties"], f"{fn['name']}: required '{req}' undeclared"
        assert fn["description"].strip()
    assert len(names) == len(set(names)) == 10
    # They must not shadow the built-in Hermes browser tools.
    for built_in in ("browser_navigate", "browser_click", "browser_type",
                     "browser_snapshot", "browser_vision", "browser_press"):
        assert built_in not in names


# ---------------------------------------------------------------------------
# 2. CLI mapping
# ---------------------------------------------------------------------------

def test_keys_real_keystrokes(ab_recorder):
    out = json.loads(_browser_keys_handler({"text": "hello"}, **KW))
    assert out["success"] is True
    assert ab_recorder == [("keyboard", ["type", "hello"])]
    assert out["url"] == "https://x.test/p"  # breadcrumb attached


def test_keys_paste_and_enter(ab_recorder):
    json.loads(_browser_keys_handler(
        {"text": "long text", "paste": True, "press_enter": True}, **KW))
    assert ab_recorder == [("keyboard", ["inserttext", "long text"]),
                           ("press", ["Enter"])]


def test_keys_requires_text(ab_recorder):
    out = json.loads(_browser_keys_handler({}, **KW))
    assert out["success"] is False and ab_recorder == []


def test_hover_dblclick_scrollintoview_drag(ab_recorder):
    _browser_hover_handler({"ref": "@e5"}, **KW)
    _browser_dblclick_handler({"ref": "@e6"}, **KW)
    _browser_scrollintoview_handler({"ref": "@e7"}, **KW)
    _browser_drag_handler({"from_ref": "@e1", "to_ref": "@e2"}, **KW)
    assert ab_recorder == [("hover", ["@e5"]), ("dblclick", ["@e6"]),
                           ("scrollintoview", ["@e7"]), ("drag", ["@e1", "@e2"])]


def test_click_xy_sequence(ab_recorder):
    out = json.loads(_browser_click_xy_handler({"x": 120, "y": 45}, **KW))
    assert out["success"] is True
    assert ab_recorder == [("mouse", ["move", "120", "45"]),
                           ("mouse", ["down"]), ("mouse", ["up"])]


def test_click_xy_double_right(ab_recorder):
    _browser_click_xy_handler({"x": 1, "y": 2, "double": True, "button": "right"}, **KW)
    assert ab_recorder == [("mouse", ["move", "1", "2"]),
                           ("mouse", ["down", "right"]), ("mouse", ["up", "right"]),
                           ("mouse", ["down", "right"]), ("mouse", ["up", "right"])]


def test_click_xy_validates(ab_recorder):
    assert json.loads(_browser_click_xy_handler({"x": "nope", "y": 2}, **KW))["success"] is False
    assert json.loads(_browser_click_xy_handler({"x": -4, "y": 2}, **KW))["success"] is False
    assert ab_recorder == []


def test_wait_caps_numeric_and_passes_selectors(ab_recorder):
    _browser_wait_handler({"for": "99999"}, **KW)
    _browser_wait_handler({"for": ".save-toast"}, **KW)
    assert ab_recorder == [("wait", ["15000"]), ("wait", [".save-toast"])]


def test_upload_preflights_files(tmp_path, ab_recorder):
    out = json.loads(_browser_upload_handler(
        {"ref": "@e3", "files": [str(tmp_path / "missing.csv")]}, **KW))
    assert out["success"] is False and "not found" in out["error"]
    f = tmp_path / "real.csv"
    f.write_text("a,b\n")
    out = json.loads(_browser_upload_handler({"ref": "@e3", "files": str(f)}, **KW))
    assert out["success"] is True
    assert ab_recorder == [("upload", ["@e3", str(f)])]


def test_action_error_passthrough(monkeypatch):
    monkeypatch.setattr(gui, "_ab",
                        lambda *a, **k: {"success": False, "error": "no session"})
    monkeypatch.setattr(gui, "_breadcrumb", lambda task_id: {})
    out = json.loads(_browser_hover_handler({"ref": "@e1"}, **KW))
    assert out["success"] is False and out["error"] == "no session"


# ---------------------------------------------------------------------------
# 3. _ab session-key routing (the one test that exercises the lazy import)
# ---------------------------------------------------------------------------

def test_ab_wraps_session_key(monkeypatch):
    seen = {}

    fake_bt = types.ModuleType("tools.browser_tool")
    fake_bt._last_session_key = lambda t: f"wrapped:{t}"
    def fake_run(task_id, command, args, timeout=None):
        seen.update(task_id=task_id, command=command, args=args)
        return {"success": True}
    fake_bt._run_browser_command = fake_run
    fake_tools = types.ModuleType("tools")
    fake_tools.browser_tool = fake_bt
    monkeypatch.setitem(sys.modules, "tools", fake_tools)
    monkeypatch.setitem(sys.modules, "tools.browser_tool", fake_bt)

    res = gui._ab("agent_name:tester", "hover", ["@e1"])
    assert res["success"] is True
    assert seen == {"task_id": "wrapped:agent_name:tester",
                    "command": "hover", "args": ["@e1"]}


# ---------------------------------------------------------------------------
# 4. Screenshot → workspace
# ---------------------------------------------------------------------------

def test_screenshot_lands_in_workspace(tmp_path, ab_recorder, monkeypatch):
    monkeypatch.setattr(gui, "_agent_workspace",
                        lambda caller: tmp_path if caller == "tester" else None)
    out = json.loads(_browser_screenshot_handler({"label": "after publish!"}, **KW))
    assert out["success"] is True
    p = Path(out["screenshot_path"])
    assert p.exists() and p.parent == tmp_path / "screenshots"
    assert "after-publish" in p.name and p.suffix == ".png"
    assert (out["width"], out["height"]) == (200, 100)
    cmd, args = ab_recorder[0]
    assert cmd == "screenshot" and "--annotate" not in args and "--full" not in args


def test_screenshot_flags(tmp_path, ab_recorder, monkeypatch):
    monkeypatch.setattr(gui, "_agent_workspace", lambda caller: tmp_path)
    _browser_screenshot_handler({"annotate": True, "full_page": True}, **KW)
    _, args = ab_recorder[0]
    assert args[0] == "--annotate" and args[1] == "--full"


def test_png_dimensions_rejects_non_png(tmp_path):
    assert _png_dimensions(tmp_path / "absent.png") is None
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"definitely not a png header....")
    assert _png_dimensions(bad) is None
    good = tmp_path / "good.png"
    good.write_bytes(_minimal_png(33, 7))
    assert _png_dimensions(good) == (33, 7)


# ---------------------------------------------------------------------------
# 5. Locate — parsing + coordinate scaling
# ---------------------------------------------------------------------------

def test_parse_locate_reply_defensive():
    assert _parse_locate_reply("```json\n{\"found\": true, \"x\": 5, \"y\": 6}\n```")["x"] == 5
    assert _parse_locate_reply("Sure! Here you go: {\"found\": false}")["found"] is False
    assert _parse_locate_reply("no json at all") is None
    assert _parse_locate_reply("{broken json") is None


def test_locate_scales_screenshot_pixels_to_css(tmp_path, monkeypatch):
    # Screenshot is 2000x1000 px but the live viewport is 1000x500 CSS px
    # (devicePixelRatio 2) — the returned click point must be halved.
    def fake_ab(task_id, command, args=None, timeout=None):
        assert command == "screenshot"
        path = Path(args[-1]); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_minimal_png(2000, 1000))
        return {"success": True, "data": {"path": str(path)}}

    monkeypatch.setattr(gui, "_ab", fake_ab)
    monkeypatch.setattr(gui, "_agent_workspace", lambda caller: tmp_path)
    monkeypatch.setattr(gui, "_breadcrumb",
                        lambda task_id: {"url": "u", "title": "t", "viewport": [1000, 500]})
    monkeypatch.setattr(gui, "_resolve_vision_endpoint",
                        lambda caller: {"base_url": "http://stub", "api_key": "k",
                                        "model": "vlm-test"})
    monkeypatch.setattr(gui, "_call_vision_model",
                        lambda endpoint, prompt, png: json.dumps(
                            {"found": True, "x": 1500, "y": 400, "confidence": 0.9,
                             "what_is_there": "blue Publish button"}))

    out = json.loads(_browser_locate_handler({"description": "blue Publish button"}, **KW))
    assert out["success"] and out["found"]
    assert (out["x"], out["y"]) == (750, 200)
    assert out["vision_model"] == "vlm-test"
    assert "browser_click_xy" in out["next"]


def test_locate_not_found_and_bad_reply(tmp_path, monkeypatch):
    def fake_shot(task_id, command, args=None, timeout=None):
        Path(args[-1]).write_bytes(_minimal_png(10, 10))
        return {"success": True, "data": {"path": str(args[-1])}}

    monkeypatch.setattr(gui, "_ab", fake_shot)
    monkeypatch.setattr(gui, "_agent_workspace", lambda caller: tmp_path)
    monkeypatch.setattr(gui, "_breadcrumb", lambda task_id: {})
    monkeypatch.setattr(gui, "_resolve_vision_endpoint",
                        lambda caller: {"base_url": "b", "api_key": "k", "model": "m"})

    monkeypatch.setattr(gui, "_call_vision_model",
                        lambda *a: '{"found": false, "what_is_there": "a cookie banner"}')
    out = json.loads(_browser_locate_handler({"description": "save button"}, **KW))
    assert out["success"] is True and out["found"] is False
    assert "x" not in out

    monkeypatch.setattr(gui, "_call_vision_model", lambda *a: "I cannot help with that")
    out = json.loads(_browser_locate_handler({"description": "save button"}, **KW))
    assert out["success"] is False and "raw_reply" in out


# ---------------------------------------------------------------------------
# 6. Vision model setting
# ---------------------------------------------------------------------------

def test_vision_model_setting_layering(monkeypatch):
    import swarm_server.config as config

    store = {"agents": {}}
    monkeypatch.setattr(config, "load_agents_config", lambda: store)
    monkeypatch.setattr(config, "_save_full_config",
                        lambda cfg: store.update(cfg))

    assert config.get_vision_model() == config.VISION_MODEL  # default falls through
    config.update_global_settings({"vision_model": "  qwen2.5-vl-72b  "})
    assert config.get_vision_model() == "qwen2.5-vl-72b"
    assert store["settings"]["vision_model"] == "qwen2.5-vl-72b"
    config.update_global_settings({"vision_model": ""})  # clearing restores default
    assert config.get_vision_model() == config.VISION_MODEL


def test_settings_defaults_include_vision_model():
    from swarm_server.config import _GLOBAL_SETTINGS_DEFAULTS

    assert "vision_model" in _GLOBAL_SETTINGS_DEFAULTS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
