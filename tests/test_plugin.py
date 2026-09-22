from __future__ import annotations

import asyncio
import copy
import json
import socket
from typing import Any

import pytest

from plugin.plugins.neko_rimworld import (
    BridgeSettings,
    NekoRimworldPlugin,
    _agent_handoff,
    _parse_settings,
    _query_summary,
)
from plugin.plugins.neko_rimworld.agent import (
    AgentAuditLedger,
    AgentAuditRegistry,
    AgentModelSettings,
    AgentRunState,
    AgentRuntime,
    build_agent_model_settings,
)
from plugin.plugins.neko_rimworld.protocol import QUERY_RESULT, Payload, SessionDescriptor, make_envelope
from plugin.plugins.neko_rimworld.query_tools import CatalogTools
from plugin.plugins.neko_rimworld.state import BridgeStateStore, DiagnosticRing

TOKEN = "0123456789abcdef0123456789abcdef"


class FakeConfig:
    def __init__(self, value: object) -> None:
        self.value = value
        self.updates: list[dict[str, Any]] = []
        self.update_error: Exception | None = None

    async def dump(self, timeout: float = 5.0) -> object:
        assert timeout > 0
        return copy.deepcopy(self.value)

    async def update(self, patch: dict[str, Any], *, timeout: float = 5.0) -> object:
        assert timeout > 0
        self.updates.append(copy.deepcopy(patch))
        if self.update_error is not None:
            raise self.update_error
        root = copy.deepcopy(self.value) if isinstance(self.value, dict) else {}
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(root.get(key), dict):
                root[key] = {**root[key], **copy.deepcopy(value)}
            else:
                root[key] = copy.deepcopy(value)
        self.value = root
        return copy.deepcopy(root)


class FakeLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _record(self, level: str, message: object, *args: object) -> None:
        text = str(message)
        for value in args:
            text = text.replace("{}", str(value), 1)
        self.records.append((level, text))

    def info(self, message: object, *args: object) -> None:
        self._record("info", message, *args)

    def warning(self, message: object, *args: object) -> None:
        self._record("warning", message, *args)

    def error(self, message: object, *args: object) -> None:
        self._record("error", message, *args)

    def exception(self, message: object, *args: object) -> None:
        self._record("exception", message, *args)


def make_plugin(config: object) -> tuple[NekoRimworldPlugin, list[dict[str, Any]]]:
    plugin = object.__new__(NekoRimworldPlugin)
    logger = FakeLogger()
    reports: list[dict[str, Any]] = []
    plugin.logger = logger
    plugin.config = FakeConfig(config)
    plugin._settings = BridgeSettings()
    plugin._agent_settings = AgentModelSettings()
    plugin._state_store = BridgeStateStore(DiagnosticRing(max_events=20))
    plugin._bridge = None
    plugin._last_dashboard = {}
    plugin._settings_action_nonce = "hosted-ui-action-nonce"
    plugin._reconfigure_lock = asyncio.Lock()
    plugin.get_logger = lambda _suffix=None: logger
    plugin.report_status = reports.append
    from plugin.plugins.neko_rimworld.reports import ReportStore
    plugin._reports = ReportStore()
    plugin._agent_audit = AgentAuditRegistry()
    plugin._catalog_tools = CatalogTools(lambda: plugin._bridge, plugin._agent_query_tool)
    from types import SimpleNamespace
    plugin._knowledge = SimpleNamespace(request_refresh=lambda: None, close=lambda: None, open_view=lambda: None)
    plugin._agent_runtime = AgentRuntime(
        observation_tools=[],
        tools_provider=plugin._load_agent_tools,
        audit_registry=plugin._agent_audit,
        logger=logger,
    )
    return plugin, reports


def reserve_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_settings_are_bounded_without_exposing_token() -> None:
    settings, warnings = _parse_settings(
        {
            "neko_rimworld": {
                "port": True,
                "pairing_token": "short",
                "diagnostic_log_capacity": 999,
            }
        }
    )
    assert settings == BridgeSettings()
    assert len(warnings) == 3
    assert TOKEN not in json.dumps({"configured": settings.token_configured})


def test_token_length_matches_csharp_utf16_contract() -> None:
    accepted, accepted_warnings = _parse_settings({"neko_rimworld": {"pairing_token": "😀" * 20}})
    rejected, rejected_warnings = _parse_settings({"neko_rimworld": {"pairing_token": "😀" * 65}})
    assert accepted.token_configured
    assert not accepted_warnings
    assert not rejected.token_configured
    assert rejected_warnings


def test_settings_action_marks_credentials_write_only_and_rejects_extra_fields() -> None:
    metadata = NekoRimworldPlugin.save_bridge_settings.__neko_event_meta__
    schema = metadata.input_schema

    assert schema["additionalProperties"] is False
    assert schema["properties"]["pairing_token"]["writeOnly"] is True
    assert schema["properties"]["ui_nonce"]["writeOnly"] is True


def test_only_consult_entry_is_visible_to_automatic_agent_routing() -> None:
    from plugin.server.application.plugins.metadata_scanner import _event_meta_payload

    consult = NekoRimworldPlugin.rimworld_consult.__neko_event_meta__
    bridge_settings = NekoRimworldPlugin.save_bridge_settings.__neko_event_meta__
    agent_settings = NekoRimworldPlugin.save_agent_settings.__neko_event_meta__
    probe = NekoRimworldPlugin.probe_game_overview.__neko_event_meta__

    assert consult.metadata.get("agent_auto") is not False
    assert consult.llm_result_fields == ["summary"]
    assert consult.metadata["llm_result_fields"] == ["summary"]
    isolated_payload = _event_meta_payload(consult)
    assert isolated_payload["metadata"]["llm_result_fields"] == ["summary"]
    assert bridge_settings.metadata["agent_auto"] is False
    assert agent_settings.metadata["agent_auto"] is False
    assert probe.metadata["agent_auto"] is False
    assert agent_settings.input_schema["properties"]["api_key"]["writeOnly"] is True
    assert agent_settings.input_schema["properties"]["ui_nonce"]["writeOnly"] is True


def test_platform_agent_filter_sees_only_consult_entry() -> None:
    from brain.task_executor import DirectTaskExecutor

    entries = []
    for method in (
        NekoRimworldPlugin.save_bridge_settings,
        NekoRimworldPlugin.save_agent_settings,
        NekoRimworldPlugin.rimworld_consult,
        NekoRimworldPlugin.rimworld_report,
        NekoRimworldPlugin.probe_game_overview,
    ):
        meta = method.__neko_event_meta__
        entries.append({"id": meta.id, "description": str(meta.description), "metadata": dict(meta.metadata)})
    executor = object.__new__(DirectTaskExecutor)

    visible = executor._agent_visible_plugin_entries({"id": "neko_rimworld", "entries": entries})

    assert [entry["id"] for entry in visible] == ["rimworld_consult", "rimworld_report"]


def query_response(status="ok", data=None, error_code=None, message=None):
    return make_envelope(
        QUERY_RESULT,
        "1" * 32,
        4,
        Payload(
            status=status,
            data={} if status == "ok" and data is None else data,
            issues=[],
            error_code=error_code,
            message=message,
        ),
        session=SessionDescriptor("2" * 32, "3" * 32, 1, 1),
        request_id="4" * 32,
    )


def test_query_summary_excludes_game_text_and_retains_response():
    result = _query_summary(query_response(data={"new_field": "untrusted name"}))
    assert "untrusted name" not in result["summary"]
    assert result["response"]["payload"]["data"]["new_field"] == "untrusted name"


def test_query_terminal_status_is_not_a_protocol_error():
    result = _query_summary(query_response("busy", error_code="game_busy", message="busy"))
    assert result["status"] == "busy"
    assert result["error_code"] == "game_busy"


@pytest.mark.asyncio
async def test_startup_without_token_keeps_dashboard_available() -> None:
    plugin, reports = make_plugin({"neko_rimworld": {"pairing_token": ""}})
    result = await plugin.startup()
    dashboard = await plugin.dashboard_ui_context()

    assert result.value["status"] == "waiting_for_configuration"
    assert dashboard["runtime"]["phase"] == "waiting_for_configuration"
    assert dashboard["configuration"]["token_configured"] is False
    assert dashboard["diagnostics"]
    assert reports[-1]["worker_alive"] is False
    assert reports[-1]["port"] == 48919
    assert set(reports[-1]) == {
        "status",
        "connected",
        "authenticated",
        "session_open",
        "protocol_version",
        "port",
        "last_error_code",
        "worker_alive",
    }


@pytest.mark.asyncio
async def test_facade_starts_and_stops_real_listener() -> None:
    port = reserve_port()
    plugin, reports = make_plugin(
        {"neko_rimworld": {"port": port, "pairing_token": TOKEN, "diagnostic_log_capacity": 20}}
    )
    started = await plugin.startup()
    try:
        assert started.value["status"] == "listening"
        assert plugin._bridge is not None and plugin._bridge.running
        assert reports[-1]["port"] == port
    finally:
        stopped = await plugin.shutdown()

    assert stopped.value["status"] == "stopped"
    assert plugin._bridge is None
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


@pytest.mark.asyncio
async def test_probe_without_listener_returns_typed_error() -> None:
    plugin, _reports = make_plugin({})
    result = await plugin.probe_game_overview()
    assert result.error.code == "bridge_not_running"


@pytest.mark.asyncio
async def test_hosted_probe_uses_colony_object_overview_section() -> None:
    calls = []
    observed = query_response(data={"o0": {"game": {"paused": True}, "maps": []}})

    class Bridge:
        def request(self, operation, arguments, timeout):
            calls.append((operation, arguments, timeout))
            return observed

    plugin, _reports = make_plugin({})
    plugin._bridge = Bridge()
    result = await plugin.probe_game_overview()

    assert calls == [("template_query", {"requests": [{"target": {"type": "colony"}, "sections": ["overview"]}]}, 2.5)]
    assert result.value["response"] == observed.to_dict()
    assert result.value["status"] == "ok"


@pytest.mark.asyncio
async def test_failed_stop_retains_bridge_handle() -> None:
    class RefusingBridge:
        running = True

        def stop(self, _timeout: float) -> bool:
            return False

    plugin, _reports = make_plugin({})
    bridge = RefusingBridge()
    plugin._bridge = bridge
    assert not await plugin._stop_existing_bridge()
    assert plugin._bridge is bridge


@pytest.mark.asyncio
async def test_save_settings_requires_hosted_ui_nonce() -> None:
    plugin, _reports = make_plugin({})
    result = await plugin.save_bridge_settings(
        port=reserve_port(),
        pairing_token=TOKEN,
        ui_nonce="wrong",
    )

    assert result.error.code == "ui_only"
    assert plugin.config.updates == []
    assert plugin._bridge is None


@pytest.mark.asyncio
async def test_first_token_is_saved_and_listener_is_reconfigured_without_disclosure() -> None:
    port = reserve_port()
    plugin, reports = make_plugin({})
    state_store = plugin._state_store
    action_nonce = plugin._settings_action_nonce

    result = await plugin.save_bridge_settings(
        port=port,
        pairing_token=TOKEN,
        ui_nonce=action_nonce,
    )
    try:
        assert result.value == {
            "status": "listening",
            "summary": f"RimWorld bridge is listening on 127.0.0.1:{port}.",
            "port": port,
            "token_configured": True,
        }
        assert plugin.config.updates == [{"neko_rimworld": {"port": port, "pairing_token": TOKEN}}]
        assert plugin._state_store is state_store
        assert plugin._bridge is not None and plugin._bridge.running
        dashboard = await plugin.dashboard_ui_context()
        public_values = json.dumps(
            {
                "result": result.value,
                "dashboard": dashboard,
                "reports": reports,
                "logs": plugin.logger.records,
            }
        )
        assert TOKEN not in public_values
        assert dashboard["action_nonce"] == plugin._settings_action_nonce
        assert plugin._settings_action_nonce != action_nonce

        replay = await plugin.save_bridge_settings(
            port=port,
            pairing_token="",
            ui_nonce=action_nonce,
        )
        assert replay.error.code == "ui_only"
        assert len(plugin.config.updates) == 1
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_blank_token_retains_saved_token_while_moving_listener() -> None:
    old_port = reserve_port()
    new_port = reserve_port()
    while new_port == old_port:
        new_port = reserve_port()
    plugin, _reports = make_plugin(
        {"neko_rimworld": {"port": old_port, "pairing_token": TOKEN, "diagnostic_log_capacity": 20}}
    )
    await plugin.startup()
    state_store = plugin._state_store

    result = await plugin.save_bridge_settings(
        port=new_port,
        pairing_token="   ",
        ui_nonce=plugin._settings_action_nonce,
    )
    try:
        assert result.value["status"] == "listening"
        assert result.value["port"] == new_port
        assert plugin.config.updates[-1] == {"neko_rimworld": {"port": new_port, "pairing_token": TOKEN}}
        assert plugin._state_store is state_store
        old_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            old_probe.bind(("127.0.0.1", old_port))
        finally:
            old_probe.close()
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_unchanged_bridge_settings_keep_the_active_listener() -> None:
    port = reserve_port()
    plugin, _reports = make_plugin(
        {"neko_rimworld": {"port": port, "pairing_token": TOKEN, "diagnostic_log_capacity": 20}}
    )
    await plugin.startup()
    bridge = plugin._bridge

    result = await plugin.save_bridge_settings(
        port=port,
        pairing_token="",
        ui_nonce=plugin._settings_action_nonce,
    )
    try:
        assert result.value["status"] == "listening"
        assert plugin._bridge is bridge
        assert plugin.config.updates == []
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_same_port_token_change_restarts_with_an_authenticated_client() -> None:
    from plugin.plugins.neko_rimworld.tests.test_bridge import connect_and_challenge, finish_authentication

    replacement_token = "fedcba9876543210fedcba9876543210"
    port = reserve_port()
    plugin, _reports = make_plugin(
        {"neko_rimworld": {"port": port, "pairing_token": TOKEN, "diagnostic_log_capacity": 20}}
    )
    await plugin.startup()
    original_bridge = plugin._bridge
    client, hello, challenge = connect_and_challenge(port)
    finish_authentication(client, hello, challenge, TOKEN)

    try:
        result = await plugin.save_bridge_settings(
            port=port,
            pairing_token=replacement_token,
            ui_nonce=plugin._settings_action_nonce,
        )
        assert result.value["status"] == "listening"
        assert plugin._bridge is not None and plugin._bridge is not original_bridge
        assert plugin._bridge.running
        assert plugin.config.value["neko_rimworld"]["pairing_token"] == replacement_token
    finally:
        client.close()
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_blank_token_is_rejected_when_no_token_exists() -> None:
    plugin, _reports = make_plugin({"neko_rimworld": {"pairing_token": ""}})

    result = await plugin.save_bridge_settings(
        port=reserve_port(),
        pairing_token="",
        ui_nonce=plugin._settings_action_nonce,
    )

    assert result.error.code == "pairing_token_required"
    assert plugin.config.updates == []
    assert plugin._bridge is None


@pytest.mark.asyncio
async def test_save_validates_port_and_utf16_token_length() -> None:
    plugin, _reports = make_plugin({})

    invalid_port = await plugin.save_bridge_settings(
        port=True,
        pairing_token=TOKEN,
        ui_nonce=plugin._settings_action_nonce,
    )
    invalid_token = await plugin.save_bridge_settings(
        port=reserve_port(),
        pairing_token="😀" * 65,
        ui_nonce=plugin._settings_action_nonce,
    )

    assert invalid_port.error.code == "invalid_port"
    assert invalid_token.error.code == "invalid_pairing_token"
    assert plugin.config.updates == []


@pytest.mark.asyncio
async def test_save_stop_failure_preserves_worker_handle_and_does_not_start_another() -> None:
    class RefusingBridge:
        running = True

        def stop(self, _timeout: float) -> bool:
            return False

    plugin, _reports = make_plugin({"neko_rimworld": {"pairing_token": TOKEN}})
    bridge = RefusingBridge()
    plugin._bridge = bridge

    result = await plugin.save_bridge_settings(
        port=reserve_port(),
        pairing_token="",
        ui_nonce=plugin._settings_action_nonce,
    )

    assert result.error.code == "bridge_stop_failed"
    assert plugin._bridge is bridge
    assert plugin.config.updates == []
    assert plugin._state_store.snapshot()["runtime"]["phase"] == "stop_failed"


@pytest.mark.asyncio
async def test_failed_rebind_releases_candidate_and_keeps_state_store() -> None:
    occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupied.bind(("127.0.0.1", 0))
    occupied.listen(1)
    port = int(occupied.getsockname()[1])
    plugin, _reports = make_plugin({"neko_rimworld": {"pairing_token": ""}})
    state_store = plugin._state_store

    try:
        result = await plugin.save_bridge_settings(
            port=port,
            pairing_token=TOKEN,
            ui_nonce=plugin._settings_action_nonce,
        )
        assert result.error.code == "bridge_start_error"
        assert plugin._bridge is None
        assert plugin._state_store is state_store
        assert plugin.config.updates == []
        assert plugin._settings.token_configured is False
        assert plugin._state_store.snapshot()["runtime"]["phase"] == "waiting_for_configuration"
    finally:
        occupied.close()

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


@pytest.mark.asyncio
async def test_config_failure_logs_only_redacted_exception_type() -> None:
    old_port = reserve_port()
    new_port = reserve_port()
    while new_port == old_port:
        new_port = reserve_port()
    plugin, _reports = make_plugin(
        {"neko_rimworld": {"port": old_port, "pairing_token": TOKEN, "diagnostic_log_capacity": 20}}
    )
    await plugin.startup()
    plugin.config.update_error = RuntimeError(f"transport echoed {TOKEN}")

    result = await plugin.save_bridge_settings(
        port=new_port,
        pairing_token="",
        ui_nonce=plugin._settings_action_nonce,
    )
    try:
        assert result.error.code == "configuration_update_failed"
        assert TOKEN not in json.dumps(plugin.logger.records)
        assert plugin._bridge is not None and plugin._bridge.running
        assert plugin._bridge.address == ("127.0.0.1", old_port)
        assert plugin._settings.port == old_port
        assert plugin.config.value["neko_rimworld"]["port"] == old_port
        assert len(plugin.config.updates) == 1

        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", new_port))
        finally:
            probe.close()
    finally:
        await plugin.shutdown()


@pytest.mark.asyncio
async def test_agent_settings_save_is_write_only_and_does_not_restart_bridge() -> None:
    plugin, reports = make_plugin({"neko_rimworld": {"pairing_token": ""}})
    api_key = "agent-api-key-that-must-not-leak"
    nonce = plugin._settings_action_nonce

    result = await plugin.save_agent_settings(
        base_url="https://example.com/v1/",
        model="example-model",
        api_key=api_key,
        ui_nonce=nonce,
    )
    dashboard = await plugin.dashboard_ui_context()

    assert result.value == {
        "status": "configured",
        "summary": "RimWorld research agent configured with model 'example-model'.",
        "base_url": "https://example.com/v1",
        "model": "example-model",
        "api_key_configured": True,
    }
    assert plugin._bridge is None
    assert reports == []
    assert plugin._agent_settings.api_key == api_key
    assert plugin._settings_action_nonce != nonce
    assert dashboard["configuration"]["agent_configured"] is True
    assert dashboard["configuration"]["agent_api_key_configured"] is True
    public_values = json.dumps(
        {
            "result": result.value,
            "dashboard": dashboard,
            "logs": plugin.logger.records,
        }
    )
    assert api_key not in public_values


@pytest.mark.asyncio
async def test_agent_settings_blank_key_preserves_existing_key() -> None:
    api_key = "existing-agent-api-key"
    plugin, _reports = make_plugin(
        {
            "neko_rimworld": {
                "agent": {
                    "base_url": "https://old.example/v1",
                    "model": "old-model",
                    "api_key": api_key,
                }
            }
        }
    )
    plugin._agent_settings = build_agent_model_settings(
        base_url="https://old.example/v1",
        model="old-model",
        api_key=api_key,
    )

    result = await plugin.save_agent_settings(
        base_url="https://new.example/v1",
        model="new-model",
        api_key="",
        ui_nonce=plugin._settings_action_nonce,
    )

    assert result.value["status"] == "configured"
    assert plugin._agent_settings.api_key == api_key
    assert plugin.config.updates[-1]["neko_rimworld"]["agent"]["api_key"] == api_key


@pytest.mark.asyncio
async def test_agent_settings_failure_does_not_log_the_api_key() -> None:
    api_key = "agent-key-that-must-stay-private"
    plugin, _reports = make_plugin({})
    plugin.config.update_error = RuntimeError(f"transport echoed api_key={api_key}")

    result = await plugin.save_agent_settings(
        base_url="https://example.com/v1",
        model="example-model",
        api_key=api_key,
        ui_nonce=plugin._settings_action_nonce,
    )

    assert result.error.code == "configuration_update_failed"
    assert api_key not in json.dumps(plugin.logger.records)
    assert not plugin._agent_settings.configured


@pytest.mark.asyncio
async def test_consult_requires_model_configuration() -> None:
    plugin, _reports = make_plugin({})
    result = await plugin.rimworld_consult(question="How is the colony?")
    assert result.error.code == "agent_not_configured"


@pytest.mark.asyncio
async def test_output_budget_save_validate_and_context():
    plugin, _ = make_plugin({})
    for value in (True, 64.5, 131073):
        result = await plugin.save_agent_settings(base_url="https://example.com/v1", model="test",
            budgets={"max_output_tokens": value}, ui_nonce=plugin._settings_action_nonce)
        assert result.error.code == "invalid_agent_settings"
    result = await plugin.save_agent_settings(base_url="https://example.com/v1", model="test",
        budgets={"max_output_tokens": 32768}, ui_nonce=plugin._settings_action_nonce)
    assert result.is_ok()
    assert plugin.config.updates[-1]["neko_rimworld"]["agent"]["max_output_tokens"] == 32768
    context = await plugin.dashboard_ui_context()
    assert context["configuration"]["agent_budgets"]["max_output_tokens"] == 32768


@pytest.mark.asyncio
async def test_consult_returns_only_independent_agent_result() -> None:
    class FakeAgentRuntime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, AgentModelSettings]] = []

        async def run(self, question: str, settings: AgentModelSettings) -> dict[str, Any]:
            self.calls.append((question, settings))
            return {
                "status": "ok",
                "answer": "The colony is stable.",
                "caveats": [],
                "game_evidence": None,
                "freshness": None,
                "audit": {"run_id": "run-1"},
            }

    plugin, _reports = make_plugin({})
    plugin._agent_settings = build_agent_model_settings(
        base_url="http://127.0.0.1:11434/v1",
        model="local-model",
    )
    runtime = FakeAgentRuntime()
    plugin._agent_runtime = runtime

    result = await plugin.rimworld_consult(question="  How is the colony?  ")

    assert result.value["answer"] == "The colony is stable."
    assert result.value["summary"] == "The colony is stable."
    assert result.value["reply"] == "The colony is stable."
    assert runtime.calls == [("How is the colony?", plugin._agent_settings)]


def test_agent_handoff_does_not_derive_agent_text_from_degraded_fields() -> None:
    from utils.result_parser import parse_plugin_result

    result = _agent_handoff(
        {
            "status": "degraded",
            "answer": "The colony snapshot is available.",
            "caveats": ["The model response was incomplete."],
            "game_evidence": {"snapshot_sequence": 7},
        }
    )

    assert "callback-safe response" in result["summary"]
    assert "snapshot_sequence" not in result["summary"]
    assert "model response was incomplete" not in result["summary"].lower()
    assert result["reply"] == result["summary"]
    assert parse_plugin_result(result, llm_result_fields=["summary"], plugin_message="done") == result["summary"]
    assert parse_plugin_result(result, llm_result_fields=None, plugin_message="done") == result["reply"]


@pytest.mark.asyncio
async def test_pending_agent_model_call_does_not_block_dashboard_context() -> None:
    started = asyncio.Event()

    class WaitingModel:
        async def complete(self, messages, tools, *, timeout: float):
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    plugin, _reports = make_plugin({})
    plugin._agent_settings = build_agent_model_settings(
        base_url="http://127.0.0.1:11434/v1",
        model="local-model",
        request_timeout_seconds=3,
        run_budget_seconds=10,
    )
    plugin._agent_audit = AgentAuditRegistry()
    plugin._agent_runtime = AgentRuntime(
        observation_tools=[],
        tools_provider=plugin._catalog_tools.load,
        audit_registry=plugin._agent_audit,
        logger=plugin.logger,
        model_factory=lambda _settings: WaitingModel(),
    )

    task = asyncio.create_task(plugin.rimworld_consult(question="How is the colony?"))
    await started.wait()
    dashboard = await asyncio.wait_for(plugin.dashboard_ui_context(), timeout=0.2)
    assert dashboard["agent"]["active_count"] == 1
    assert dashboard["agent"]["latest_run"]["state"] == "model_requesting"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert plugin._agent_runtime.snapshot()["active_count"] == 0


@pytest.mark.asyncio
async def test_dashboard_is_lightweight_and_details_preserve_selected_audit() -> None:
    plugin, _reports = make_plugin({})
    ledger = AgentAuditLedger("inspect nested audit")
    plugin._agent_audit.start(ledger)
    ledger.transition(
        AgentRunState.MODEL_REQUESTING,
        "model_request_started",
        {"messages": [{"role": "user", "content": "exact request content"}]},
    )
    ledger.transition(
        AgentRunState.MODEL_RESPONDED,
        "model_response_received",
        {"content": "exact model output", "tool_calls": []},
    )
    ledger.transition(AgentRunState.FINALIZING, "final_received", {"answer": "exact model output"})
    ledger.transition(AgentRunState.COMPLETED, "run_completed")
    plugin._agent_audit.finish(ledger)

    dashboard = await plugin.dashboard_ui_context()
    encoded = json.dumps(dashboard["agent"], ensure_ascii=False)

    assert "exact request content" not in encoded
    assert "exact model output" not in encoded
    assert "events" not in dashboard["agent"]["latest_run"]
    details = await plugin.get_agent_run_details(ledger.run_id)
    assert "exact request content" in json.dumps(details.value)
    assert "exact model output" in json.dumps(details.value)
    assert details.value["run_id"] == ledger.run_id


def test_manifest_entries_support_discovery_without_importing_plugin_class():
    import tomllib
    from pathlib import Path

    from plugin.core.registry import _extract_entries_preview
    from plugin.server.application.plugins.ui_query_service import _entry_ids_from_meta

    manifest = tomllib.loads((Path(__file__).parents[1] / "plugin.toml").read_text(encoding="utf-8"))
    static = _extract_entries_preview(
        "neko_rimworld", type("UnscannedPluginStub", (), {}), manifest, manifest["plugin"]
    )
    decorated = _extract_entries_preview("neko_rimworld", NekoRimworldPlugin, {}, {})
    actual = {entry["id"]: entry for entry in static}
    expected = {entry["id"]: entry for entry in decorated}
    assert actual.keys() == expected.keys()
    for entry_id in expected:
        for key in ("name", "description", "input_schema", "timeout", "llm_result_fields", "metadata"):
            default = [] if key == "llm_result_fields" else None
            assert actual[entry_id].get(key, default) == expected[entry_id].get(key, default), (entry_id, key)
    assert "rimworld_consult" in _entry_ids_from_meta({"entries_preview": static})
    assert [entry["id"] for entry in static if entry.get("metadata", {}).get("agent_auto") is not False] == [
        "rimworld_consult", "rimworld_report"
    ]


@pytest.mark.asyncio
async def test_run_detail_selection_paging_and_expired_records():
    plugin, _ = make_plugin({})
    plugin._agent_audit = AgentAuditRegistry(max_recent_runs=1)
    first = AgentAuditLedger("first")
    plugin._agent_audit.start(first)
    for index in range(60):
        first.record("event", {"value": index})
    plugin._agent_audit.finish(first)
    page = (await plugin.get_agent_run_details(first.run_id, event_offset=50, event_limit=10)).value
    assert page["run_id"] == first.run_id and len(page["events"]) == 10
    assert page["events"][0]["event_seq"] == 51 and page["events_page"]["has_more"]
    second = AgentAuditLedger("second")
    plugin._agent_audit.start(second)
    assert (await plugin.get_agent_run_details(first.run_id)).value["question"] == "first"
    plugin._agent_audit.finish(second)
    assert (await plugin.get_agent_run_details(first.run_id)).error.code == "run_not_retained"
    assert (await plugin.get_agent_run_details(second.run_id, event_offset=-1)).error.code == "invalid_event_page"


@pytest.mark.asyncio
async def test_budget_settings_persist_validate_and_keep_existing_credentials():
    plugin, _ = make_plugin({})
    plugin._agent_settings = build_agent_model_settings(
        base_url="https://example.com/v1", model="test", api_key="private-key"
    )
    result = await plugin.save_agent_settings(
        base_url="https://example.com/v1",
        model="test",
        api_key="",
        ui_nonce=plugin._settings_action_nonce,
        budgets={"request_timeout_seconds": 40, "run_budget_seconds": 80, "first_response_timeout_seconds": 30},
    )
    assert result.value["status"] == "configured"
    assert plugin._agent_settings.request_timeout_seconds == 40
    assert plugin._agent_settings.run_budget_seconds == 80
    assert plugin._agent_settings.api_key == "private-key"
    assert plugin.config.updates[-1]["neko_rimworld"]["agent"]["first_response_timeout_seconds"] == 30
    updates = len(plugin.config.updates)
    failed = await plugin.save_agent_settings(
        base_url="https://example.com/v1",
        model="test",
        ui_nonce=plugin._settings_action_nonce,
        budgets={"run_budget_seconds": 900},
    )
    assert failed.error.code == "invalid_agent_settings" and len(plugin.config.updates) == updates
    dashboard = await plugin.dashboard_ui_context()
    assert dashboard["configuration"]["agent_budgets"]["request_timeout_seconds"] == 40
    assert "private-key" not in json.dumps(dashboard)
