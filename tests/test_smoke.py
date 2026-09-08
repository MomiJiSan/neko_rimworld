import tomllib
from pathlib import Path


def test_plugin_manifest_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = root / "plugin.toml"
    assert manifest.is_file()
    text = manifest.read_text(encoding="utf-8")
    assert 'id = "neko_rimworld"' in text
    assert 'entry = "plugin.plugins.neko_rimworld:NekoRimworldPlugin"' in text
    assert 'entry = "ui/panel.tsx"' in text
    assert 'mode = "hosted-tsx"' in text
    assert 'permissions = ["state:read", "config:write", "action:call"]' in text

    parsed = tomllib.loads(text)
    assert parsed["neko_rimworld"]["pairing_token"] == ""
    assert parsed["neko_rimworld"]["agent"]["api_key"] == ""
    assert parsed["neko_rimworld"]["agent"]["base_url"] == ""
    assert parsed["neko_rimworld"]["agent"]["model"] == ""
    assert "config:read" not in parsed["plugin"]["ui"]["panel"][0]["permissions"]

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == parsed["plugin"]["version"] == "0.8.0"
    assert "httpx>=0.28.1,<0.29" in pyproject["project"]["dependencies"]


def test_hosted_panel_exists() -> None:
    root = Path(__file__).resolve().parents[1]
    panel = root / "ui" / "panel.tsx"
    assert panel.is_file()
    source = panel.read_text(encoding="utf-8")
    assert 'from "@neko/plugin-ui"' in source
    assert "ErrorBoundary" in source
    assert "Tabs" in source
    for name in ("home", "sessions", "debug", "settings"):
        assert (root / "ui" / "pages" / f"{name}.tsx").is_file()
    controller = (root / "ui" / "controller.ts").read_text(encoding="utf-8")
    for action in (
        "probe_game_overview",
        "save_bridge_settings",
        "save_agent_settings",
        "rimworld_consult",
        "get_agent_run_details",
    ):
        assert action in controller
    assert "PasswordInput" in (root / "ui" / "pages" / "settings.tsx").read_text(encoding="utf-8")
