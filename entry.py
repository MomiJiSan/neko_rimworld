from __future__ import annotations

import asyncio
import copy
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
    ui,
)

from .agent import (
    AgentAuditRegistry,
    AgentConfigurationError,
    AgentModelSettings,
    AgentRuntime,
    ToolExecutionError,
    attach_agent_handoff,
    build_agent_model_settings,
    parse_agent_model_settings,
    semantic_interaction_factory,
)
from .agent.handoff import count_handoff_tokens
from .workspace.history_tools import make_history_tool
from .agent.tools import ToolSetup
from .agent.wiki_tools import compose_wiki_tools, knowledge_directory
from .agent.mechanism_tools import compose_implementation_knowledge
from .bridge import (
    BridgeBusyError,
    BridgeError,
    BridgeNotConnected,
    BridgeProtocolError,
    BridgeQueryTimeout,
    BridgeServer,
    BridgeStartError,
    BridgeUnsupportedError,
)
from .knowledge import KnowledgeLibrary
from .observation import Observation
from .protocol import Envelope
from .query_tools import CatalogTools
from .reports import ReportError, ReportStore
from .state import BridgeStateStore, BridgeStatus, DiagnosticRing, json_safe_copy, utc_now
from .workspace.service import NoCurrentWork, WorkspaceService
from .workspace.views import WorkspaceViews

PLUGIN_VERSION = "1.0.0"
DEFAULT_PORT = 48919
DEFAULT_DIAGNOSTIC_CAPACITY = 200
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 128
# Cooperative world/action reads have an eight-second game-side lifetime.
# The transport must allow that operation to return its partial result.
FRAME_READ_OPERATIONS = frozenset({
    "survey_space", "inspect_route", "inspect_state", "inspect_actions", "inspect_threats",
})


@dataclass(frozen=True, slots=True)
class BridgeSettings:
    port: int = DEFAULT_PORT
    pairing_token: str = ""
    diagnostic_log_capacity: int = DEFAULT_DIAGNOSTIC_CAPACITY

    @property
    def token_configured(self) -> bool:
        try:
            length = len(self.pairing_token.encode("utf-16-le", "strict")) // 2
        except UnicodeEncodeError:
            return False
        return MIN_TOKEN_LENGTH <= length <= MAX_TOKEN_LENGTH


def _parse_settings(config: object) -> tuple[BridgeSettings, list[str]]:
    warnings: list[str] = []
    root = config if isinstance(config, Mapping) else {}
    raw = root.get("neko_rimworld")
    section = raw if isinstance(raw, Mapping) else {}

    port_value = section.get("port", DEFAULT_PORT)
    if isinstance(port_value, bool) or not isinstance(port_value, int) or not 1 <= port_value <= 65_535:
        warnings.append(f"Invalid port; using {DEFAULT_PORT}.")
        port = DEFAULT_PORT
    else:
        port = port_value

    token_value = section.get("pairing_token", "")
    token = token_value.strip() if isinstance(token_value, str) else ""
    token_candidate = BridgeSettings(pairing_token=token)
    if token and not token_candidate.token_configured:
        warnings.append("Pairing token must contain 32-128 characters; listener was not started.")
        token = ""

    capacity_value = section.get("diagnostic_log_capacity", DEFAULT_DIAGNOSTIC_CAPACITY)
    if (
        isinstance(capacity_value, bool)
        or not isinstance(capacity_value, int)
        or not 20 <= capacity_value <= DiagnosticRing.MAX_EVENTS
    ):
        warnings.append(f"Invalid diagnostic_log_capacity; using {DEFAULT_DIAGNOSTIC_CAPACITY}.")
        capacity = DEFAULT_DIAGNOSTIC_CAPACITY
    else:
        capacity = capacity_value

    return BridgeSettings(port, token, capacity), warnings


def _query_summary(response: Envelope) -> dict[str, Any]:
    payload = response.payload.to_dict()
    status = payload.get("status", "failed")
    return {
        "status": status,
        "summary": f"Game query {status}; {len(payload.get('issues', []))} issues.",
        "error_code": payload.get("error_code"),
        "response": response.to_dict(),
    }


def _agent_handoff(result: object) -> dict[str, Any]:
    """Add the single text projection consumed by N.E.K.O's Agent callback."""

    return attach_agent_handoff(result)


def _consultation_handoff(result, question):
    """Carry operational state through the host's bounded text projection."""
    handoff = _agent_handoff(result)
    chinese = any("\u4e00" <= c <= "\u9fff" for c in question)
    notices = []
    if result.get("status") != "ok":
        code = result.get("audit", {}).get("error_code")
        reasons = {
            "agent_run_budget": ("调查超时，未完整完成。", "Consultation incomplete: time limit."),
            "world_changed": ("游戏世界已改变，需重新咨询。", "The game world changed; consult again."),
            "agent_input_budget": ("输入预算已耗尽，调查未完整完成。", "Consultation incomplete: input limit."),
        }
        message = reasons.get(
            code, ("调查未完整完成，请检查插件面板。", "Consultation incomplete; check the plugin panel.")
        )
        notices.append(message[0 if chinese else 1])
    if result.get("workspace", {}).get("history_saved") is False:
        notices.append("历史未保存。" if chinese else "History not saved.")
    if result.get("continuity", {}).get("bound") is False:
        notices.append(
            "此轮为独立咨询，未接入持续工作。" if chinese else "Standalone consultation; no ongoing workspace attached."
        )
    if notices:
        notice = " ".join(notices)
        summary = notice + " " + handoff["summary"]
        if count_handoff_tokens(summary)[0] > 200:
            summary = notice + (
                " 请在插件面板查看结果与证据。" if chinese else " Check the plugin panel for results and evidence."
            )
        handoff["summary"] = handoff["reply"] = summary
        tokens, tokenizer = count_handoff_tokens(summary)
        handoff["delivery"].update(handoff_tokens=tokens, tokenizer=tokenizer, operational_notice=True)
    return handoff


@neko_plugin
class NekoRimworldPlugin(NekoPluginBase):
    """Read-only RimWorld bridge with a Hosted TSX diagnostics surface."""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._settings = BridgeSettings()
        self._agent_settings = AgentModelSettings()
        self._state_store = BridgeStateStore()
        self._bridge: BridgeServer | None = None
        self._last_dashboard: dict[str, Any] = {}
        self._settings_action_nonce = secrets.token_urlsafe(32)
        self._reconfigure_lock = asyncio.Lock()
        self._agent_audit = AgentAuditRegistry()
        self._reports = ReportStore(self.data_path("reports"))
        self._catalog_tools = CatalogTools(lambda: self._bridge, self._agent_query_tool)
        self._knowledge_directory = knowledge_directory()
        self._knowledge = KnowledgeLibrary(
            self._knowledge_directory, self.data_path("knowledge-index"), allow_legacy_import=False
        )
        self._agent_runtime = AgentRuntime(
            observation_tools=[],
            tools_provider=self._load_agent_tools,
            audit_registry=self._agent_audit,
            logger=self.get_logger("agent"),
            report_sink=self._save_report,
            # The semantic surface replaces the Mod's query vocabulary with a
            # closed enum, which removes the 48,927-character field catalogue
            # from every request. It declines on its own when the deployment
            # has no compilable directory, or when an
            # attached historical report needs the Mod's own tools.
            interaction_factory=semantic_interaction_factory,
        )
        self._workspace_service = None
        self._consultation_tasks = set()

    @lifecycle(id="startup")
    async def startup(self, **_: object):
        if getattr(self._knowledge, "closed", False):
            self._knowledge = KnowledgeLibrary(
                self._knowledge_directory, self.data_path("knowledge-index"), allow_legacy_import=False
            )
        self._knowledge.request_refresh()
        async with self._reconfigure_lock:
            if not await self._stop_existing_bridge():
                self._state_store.transition(
                    BridgeStatus.ERROR,
                    runtime={"phase": "stop_failed", "worker_alive": True},
                )
                self._publish_coarse_status()
                return Ok({"status": "stop_failed"})
            config_error: Exception | None = None
            try:
                config = await self.config.dump(timeout=5.0)
            except Exception as exception:
                config = {}
                config_error = exception

            settings, warnings = _parse_settings(config)
            agent_settings, agent_warnings = parse_agent_model_settings(config)
            self._agent_settings = agent_settings
            warnings.extend(agent_warnings)
            self._state_store = BridgeStateStore(DiagnosticRing(settings.diagnostic_log_capacity))

            if config_error is not None:
                incident = self._state_store.record_incident(
                    "configuration",
                    "Plugin configuration could not be loaded; safe defaults were used.",
                    level="error",
                )
                self.logger.error(
                    "[startup] config load failed; safe defaults active incident_id={} error_type={}",
                    incident,
                    type(config_error).__name__,
                )

            for warning in warnings:
                self._state_store.diagnostics.record("warning", "configuration", warning)
                self.logger.warning("[startup] {}", warning)

            return Ok(await self._start_configured_bridge(settings, source="startup"))

    @lifecycle(id="shutdown")
    async def shutdown(self, **_: object):
        calls = [task for task in self._consultation_tasks if task is not asyncio.current_task()]
        for task in calls:
            task.cancel()
        if calls:
            await asyncio.gather(*calls, return_exceptions=True)
        await self._agent_runtime.cancel_all()
        workspace = getattr(self, "_workspace_service", None)
        if workspace is not None:
            await workspace.close()
        self._knowledge.close()
        async with self._reconfigure_lock:
            stopped = await self._stop_existing_bridge()
            self._state_store.transition(
                BridgeStatus.STOPPED if stopped else BridgeStatus.ERROR,
                runtime={"phase": "stopped" if stopped else "stop_failed", "worker_alive": not stopped},
                endpoint=None,
                connection=None,
                session=None,
            )
            self._state_store.diagnostics.record(
                "info" if stopped else "error",
                "lifecycle",
                "N.E.K.O RimWorld plugin stopped." if stopped else "Bridge worker did not stop cleanly.",
            )
            self._publish_coarse_status()
            return Ok({"status": "stopped" if stopped else "stop_failed"})

    @ui.context(id="dashboard", title="RimWorld Bridge")
    async def dashboard_ui_context(self, **_: object) -> dict[str, Any]:
        try:
            snapshot = self._state_store.snapshot()
            runtime = snapshot.get("runtime")
            runtime_state = dict(runtime) if isinstance(runtime, Mapping) else {}
            runtime_state["worker_alive"] = bool(self._bridge and self._bridge.running)
            snapshot["runtime"] = runtime_state
            snapshot["configuration"] = {
                "token_configured": self._settings.token_configured,
                "agent_configured": self._agent_settings.configured,
                "agent_api_key_configured": self._agent_settings.api_key_configured,
                "agent_base_url": self._agent_settings.base_url,
                "agent_model": self._agent_settings.model,
                "agent_budgets": {
                    key: getattr(self._agent_settings, key)
                    for key in (
                        "connect_timeout_seconds",
                        "first_response_timeout_seconds",
                        "stream_idle_timeout_seconds",
                        "request_timeout_seconds",
                        "run_budget_seconds",
                        "finalization_reserve_seconds",
                        "max_output_tokens",
                    )
                },
            }
            agent_snapshot = self._agent_runtime.snapshot(include_latest_details=False)
            # This process-local nonce makes the credential action callable only by
            # this Hosted UI surface. It is intentionally not displayed or logged.
            snapshot["action_nonce"] = self._settings_action_nonce
            safe = json_safe_copy(snapshot)
            # Keep the lightweight agent summaries under the audit sanitizer's contract.
            # Full run data is read on demand via get_agent_run_details.
            safe["agent"] = agent_snapshot
            safe["consultations_in_progress"] = len(self._consultation_tasks)
            work = self._workspace_service
            safe["workspaces"] = {
                "revision": work.revision if work is not None else 0,
                "active_count": len(work.active) if work is not None else 0,
            }
            self._last_dashboard = copy.deepcopy(safe)
            return safe
        except Exception:
            incident = self._state_store.record_incident(
                "ui_context",
                "The diagnostics snapshot could not be rendered.",
                level="error",
            )
            self.logger.exception("[ui] dashboard snapshot failed incident_id={}", incident)
            fallback = copy.deepcopy(self._last_dashboard)
            fallback.setdefault("status", BridgeStatus.ERROR)
            fallback["panel_error"] = {
                "incident_id": incident,
                "message": "The diagnostics snapshot failed; showing the last safe state.",
            }
            fallback["action_nonce"] = self._settings_action_nonce
            fallback["updated_at"] = utc_now()
            return json_safe_copy(fallback)

    @ui.action(id="workspace_view", label="Read colony work", refresh_context=False)
    @plugin_entry(
        id="workspace_view",
        name="Read persistent colony work",
        description="Hosted UI read-only access to saved work, exchanges, evidence and audit.",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "work", "turn", "observation", "audit"]},
                "workspace_id": {"type": "string"},
                "turn_id": {"type": "string"},
                "observation_id": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        timeout=8.0,
        metadata={"agent_auto": False},
    )
    async def workspace_view(
        self, action="list", workspace_id="", turn_id="", observation_id="", offset=0, limit=20, **_
    ):
        try:
            view = await asyncio.to_thread(
                WorkspaceViews(self.data_path("workspaces") / "work.sqlite").read,
                action,
                workspace_id=workspace_id,
                turn_id=turn_id,
                observation_id=observation_id,
                offset=offset,
                limit=limit,
            )
            service = self._workspace_service
            active = self._agent_runtime.snapshot(include_latest_details=False)["active_runs"]

            def execution(wid):
                running = next((r for r in active if r.get("workspace_id") == wid), None)
                return running or (service.active.get(wid) if service is not None else None)

            if action == "list":
                game = getattr(self._bridge, "session", None)
                for row in view["workspaces"]:
                    row["active"] = execution(row["workspace_id"])
                    row["current_colony"] = game is not None and row["colony_id"] == game.colony_id
            else:
                view["active"] = execution(workspace_id)
            if action == "turn":
                pending = service.agent.pending_result(workspace_id, turn_id) if service is not None else None
                view["history_saved"] = view["result"] is not None and view["status"] not in {"pending", "running"}
                if pending is not None:
                    view["result"] = pending
                report_id = (view.get("result") or {}).get("report", {}).get("report_id")
                report_id = report_id or ((view.get("handoff") or {}).get("report") or {}).get("report_id")
                if report_id:
                    try:
                        record = await asyncio.to_thread(self._reports.read, report_id)
                        if record.get("workspace_id") != workspace_id:
                            raise ReportError("report_not_found", "The report belongs to another work")
                        view["report"] = {**ReportStore.metadata(record), "full_text": record["full_text"]}
                    except (ReportError, OSError):
                        view["report_unavailable"] = True
            return Ok(view)
        except (ValueError, OSError, sqlite3.Error) as error:
            return Err(SdkError(str(error), code="workspace_view_unavailable"))

    @ui.action(id="get_agent_run_details", label="Read agent run details", refresh_context=False)
    @plugin_entry(
        id="get_agent_run_details",
        name="Read retained agent run",
        description="Hosted UI access to one retained run and a bounded page of audit events.",
        input_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "event_offset": {"type": "integer", "minimum": 0, "maximum": 512},
                "event_limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["run_id"],
            "additionalProperties": False,
        },
        timeout=5.0,
        metadata={"agent_auto": False},
    )
    async def get_agent_run_details(self, run_id: str = "", event_offset: int = 0, event_limit: int = 50, **_: object):
        if not isinstance(run_id, str) or not run_id or len(run_id) > 128:
            return Err(SdkError("A valid run ID is required.", code="invalid_run_id"))
        if (
            type(event_offset) is not int
            or not 0 <= event_offset <= 512
            or type(event_limit) is not int
            or not 1 <= event_limit <= 100
        ):
            return Err(SdkError("Invalid event page.", code="invalid_event_page"))
        details = self._agent_audit.get_details(run_id, event_offset, event_limit)
        if details is None:
            return Err(
                SdkError("This run is no longer retained (history eviction or plugin reload).", code="run_not_retained")
            )
        return Ok(details)

    @ui.action(
        id="save_bridge_settings",
        label="Save bridge settings",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_bridge_settings",
        name="Save RimWorld bridge settings",
        description="Save local bridge credentials and immediately restart the loopback listener.",
        input_schema={
            "type": "object",
            "properties": {
                "port": {"type": "integer", "minimum": 1, "maximum": 65_535},
                "pairing_token": {
                    "type": "string",
                    "writeOnly": True,
                    "maxLength": MAX_TOKEN_LENGTH,
                    "description": "Leave empty to retain the currently configured token.",
                },
                "ui_nonce": {
                    "type": "string",
                    "writeOnly": True,
                    "minLength": 1,
                },
            },
            "required": ["port", "pairing_token", "ui_nonce"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=15.0,
        metadata={"agent_auto": False},
    )
    async def save_bridge_settings(
        self,
        port: int = DEFAULT_PORT,
        pairing_token: str = "",
        ui_nonce: str = "",
        **_: object,
    ):
        if not self._valid_settings_ui_nonce(ui_nonce):
            return Err(
                SdkError(
                    "Open or refresh this plugin's Hosted UI before changing bridge credentials.",
                    code="ui_only",
                )
            )
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65_535:
            return Err(SdkError("Port must be an integer from 1 to 65535.", code="invalid_port"))
        if not isinstance(pairing_token, str):
            return Err(SdkError("Pairing token must be text.", code="invalid_pairing_token"))

        supplied_token = pairing_token.strip()
        if supplied_token and not BridgeSettings(pairing_token=supplied_token).token_configured:
            return Err(
                SdkError(
                    "Pairing token must contain 32-128 UTF-16 code units.",
                    code="invalid_pairing_token",
                )
            )

        async with self._reconfigure_lock:
            try:
                current_config = await self.config.dump(timeout=5.0)
            except Exception as exception:
                incident = self._record_unexpected(
                    "settings_config_read",
                    "Bridge settings could not be read.",
                    exception,
                )
                return Err(
                    SdkError(
                        "Bridge settings could not be read.",
                        code="configuration_read_failed",
                        incident_id=incident,
                    )
                )

            current_settings, _warnings = _parse_settings(current_config)
            effective_token = supplied_token or current_settings.pairing_token
            if not BridgeSettings(pairing_token=effective_token).token_configured:
                return Err(
                    SdkError(
                        "Enter a 32-128 character pairing token before saving.",
                        code="pairing_token_required",
                    )
                )

            next_settings = BridgeSettings(
                port=port,
                pairing_token=effective_token,
                diagnostic_log_capacity=current_settings.diagnostic_log_capacity,
            )
            bridge = self._bridge
            if self._settings == next_settings and bridge is not None and bridge.running:
                active_port = bridge.address[1]
                self._settings_action_nonce = secrets.token_urlsafe(32)
                return Ok(
                    {
                        "status": "listening",
                        "summary": f"RimWorld bridge is already listening on 127.0.0.1:{active_port}.",
                        "port": active_port,
                        "token_configured": True,
                    }
                )
            if not await self._stop_existing_bridge():
                self._state_store.transition(
                    BridgeStatus.ERROR,
                    runtime={"phase": "stop_failed", "worker_alive": True},
                )
                self._state_store.diagnostics.record(
                    "error",
                    "configuration",
                    "New settings were not saved because the existing bridge worker did not stop.",
                )
                self._publish_coarse_status()
                return Err(
                    SdkError(
                        "New settings were not saved; the existing listener could not be stopped and was retained.",
                        code="bridge_stop_failed",
                        new_settings_saved=False,
                        previous_listener_retained=True,
                    )
                )

            activation = await self._start_configured_bridge(next_settings, source="settings")
            if activation["status"] != "listening":
                restored, restoration = await self._restore_previous_settings(
                    current_settings,
                    candidate_status=str(activation["status"]),
                )
                error_code = "bridge_stop_failed" if activation["status"] == "stop_failed" else "bridge_start_error"
                restoration_text = (
                    "The previous bridge state was restored."
                    if restored
                    else "The previous bridge state could not be restored."
                )
                return Err(
                    SdkError(
                        f"New settings were not saved because the candidate listener failed to start. {restoration_text}",
                        code=error_code,
                        incident_id=activation.get("incident_id"),
                        new_settings_saved=False,
                        previous_state_restored=restored,
                        previous_state=restoration.get("status"),
                        restore_incident_id=restoration.get("incident_id"),
                    )
                )

            try:
                await self.config.update(
                    {
                        "neko_rimworld": {
                            "port": next_settings.port,
                            "pairing_token": next_settings.pairing_token,
                        }
                    },
                    timeout=5.0,
                )
            except Exception as exception:
                incident = self._record_unexpected(
                    "settings_config_write",
                    "Bridge settings could not be saved.",
                    exception,
                )
                if not await self._stop_existing_bridge():
                    self._state_store.transition(
                        BridgeStatus.ERROR,
                        runtime={"phase": "rollback_stop_failed", "worker_alive": True},
                    )
                    self._state_store.diagnostics.record(
                        "error",
                        "configuration",
                        "New settings were not saved and the candidate listener could not be stopped.",
                    )
                    self._publish_coarse_status()
                    return Err(
                        SdkError(
                            "New settings were not saved; the candidate listener could not be stopped, so the previous listener was not restored.",
                            code="configuration_update_failed",
                            incident_id=incident,
                            new_settings_saved=False,
                            previous_state_restored=False,
                            candidate_listener_retained=True,
                        )
                    )

                restored, restoration = await self._restore_previous_settings(
                    current_settings,
                    candidate_status="stopped",
                )
                restoration_text = (
                    "The previous bridge state was restored."
                    if restored
                    else "The previous bridge state could not be restored."
                )
                return Err(
                    SdkError(
                        f"New settings were not saved because configuration persistence failed. {restoration_text}",
                        code="configuration_update_failed",
                        incident_id=incident,
                        new_settings_saved=False,
                        previous_state_restored=restored,
                        previous_state=restoration.get("status"),
                        restore_incident_id=restoration.get("incident_id"),
                    )
                )

            self._settings_action_nonce = secrets.token_urlsafe(32)
            return Ok(
                {
                    "status": "listening",
                    "summary": f"RimWorld bridge is listening on 127.0.0.1:{activation['port']}.",
                    "port": activation["port"],
                    "token_configured": True,
                }
            )

    @ui.action(
        id="save_agent_settings",
        label="Save agent settings",
        tone="success",
        refresh_context=True,
    )
    @plugin_entry(
        id="save_agent_settings",
        name="Save RimWorld research agent settings",
        description="Hosted UI configuration action for the plugin-owned model client.",
        input_schema={
            "type": "object",
            "properties": {
                "base_url": {"type": "string", "minLength": 1, "maxLength": 2048},
                "model": {"type": "string", "minLength": 1, "maxLength": 256},
                "api_key": {
                    "type": "string",
                    "writeOnly": True,
                    "maxLength": 4096,
                    "description": "Leave empty to retain the currently configured API key.",
                },
                "ui_nonce": {"type": "string", "writeOnly": True, "minLength": 1},
                "budgets": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "connect_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 30},
                        "first_response_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
                        "stream_idle_timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60},
                        "request_timeout_seconds": {"type": "number", "minimum": 3, "maximum": 60},
                        "run_budget_seconds": {"type": "number", "minimum": 10, "maximum": 90},
                        "finalization_reserve_seconds": {"type": "number", "minimum": 0, "maximum": 30},
                        "max_output_tokens": {"type": "integer", "minimum": 64, "maximum": 131072},
                    },
                },
            },
            "required": ["base_url", "model", "api_key", "ui_nonce"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=15.0,
        metadata={"agent_auto": False},
    )
    async def save_agent_settings(
        self,
        base_url: str = "",
        model: str = "",
        api_key: str = "",
        ui_nonce: str = "",
        budgets: dict[str, Any] | None = None,
        **_: object,
    ):
        if not self._valid_settings_ui_nonce(ui_nonce):
            return Err(
                SdkError(
                    "Open or refresh this plugin's Hosted UI before changing agent credentials.",
                    code="ui_only",
                )
            )
        if not isinstance(base_url, str) or not isinstance(model, str) or not isinstance(api_key, str):
            return Err(SdkError("Agent settings must be text values.", code="invalid_agent_settings"))
        allowed_budgets = {
            "connect_timeout_seconds",
            "first_response_timeout_seconds",
            "stream_idle_timeout_seconds",
            "request_timeout_seconds",
            "run_budget_seconds",
            "finalization_reserve_seconds",
            "max_output_tokens",
        }
        if budgets is not None and (not isinstance(budgets, dict) or set(budgets) - allowed_budgets):
            return Err(SdkError("Unsupported budget fields.", code="invalid_agent_settings"))
        selected_budgets = {key: getattr(self._agent_settings, key) for key in allowed_budgets}
        selected_budgets.update(budgets or {})
        effective_key = api_key.strip() or self._agent_settings.api_key
        try:
            next_settings = build_agent_model_settings(
                base_url=base_url,
                model=model,
                api_key=effective_key,
                request_timeout_seconds=selected_budgets["request_timeout_seconds"],
                run_budget_seconds=selected_budgets["run_budget_seconds"],
                preferred_cycles=self._agent_settings.preferred_cycles,
                max_output_tokens=selected_budgets["max_output_tokens"],
                temperature=self._agent_settings.temperature,
                connect_timeout_seconds=selected_budgets["connect_timeout_seconds"],
                first_response_timeout_seconds=selected_budgets["first_response_timeout_seconds"],
                stream_idle_timeout_seconds=selected_budgets["stream_idle_timeout_seconds"],
                finalization_reserve_seconds=selected_budgets["finalization_reserve_seconds"],
                stream_include_usage=self._agent_settings.stream_include_usage,
            )
        except AgentConfigurationError as exception:
            return Err(SdkError(str(exception), code="invalid_agent_settings"))

        try:
            await self.config.update(
                {
                    "neko_rimworld": {
                        "agent": {
                            "base_url": next_settings.base_url,
                            "model": next_settings.model,
                            "api_key": next_settings.api_key,
                            "request_timeout_seconds": next_settings.request_timeout_seconds,
                            "run_budget_seconds": next_settings.run_budget_seconds,
                            "preferred_cycles": next_settings.preferred_cycles,
                            "max_output_tokens": next_settings.max_output_tokens,
                            "temperature": next_settings.temperature,
                            "connect_timeout_seconds": next_settings.connect_timeout_seconds,
                            "first_response_timeout_seconds": next_settings.first_response_timeout_seconds,
                            "stream_idle_timeout_seconds": next_settings.stream_idle_timeout_seconds,
                            "finalization_reserve_seconds": next_settings.finalization_reserve_seconds,
                            "stream_include_usage": next_settings.stream_include_usage,
                        }
                    }
                },
                timeout=5.0,
            )
        except Exception as exception:
            incident = self._record_unexpected(
                "agent_settings_write",
                "Agent settings could not be saved.",
                exception,
            )
            return Err(
                SdkError(
                    "Agent settings could not be saved.",
                    code="configuration_update_failed",
                    incident_id=incident,
                )
            )

        self._agent_settings = next_settings
        self._settings_action_nonce = secrets.token_urlsafe(32)
        return Ok(
            {
                "status": "configured",
                "summary": f"RimWorld research agent configured with model '{next_settings.model}'.",
                "base_url": next_settings.base_url,
                "model": next_settings.model,
                "api_key_configured": next_settings.api_key_configured,
            }
        )

    @ui.action(
        id="rimworld_consult",
        label="Run agent test",
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="rimworld_consult",
        name="Consult RimWorld specialist",
        description=(
            "Ask the RimWorld specialist a question in natural language. It investigates the colony, "
            "continues relevant previous work, and retrieves historical reports and evidence when needed. "
            "Express changes of topic, goals or requested scope in the question."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "The user's RimWorld question, preserving their language and intent.",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=100.0,
        metadata={"llm_result_fields": ["summary"]},
    )
    async def rimworld_consult(self, question: str = "", *, _ctx: object = None):
        task = asyncio.current_task()
        self._consultation_tasks.add(task)
        try:
            return await self._consultation(question, _ctx)
        finally:
            self._consultation_tasks.discard(task)

    async def _consultation(self, question, _ctx):
        if not isinstance(question, str) or not question.strip():
            return Err(SdkError("A RimWorld question is required.", code="invalid_question"))
        normalized = question.strip()
        if len(normalized) > 2000:
            return Err(SdkError("The RimWorld question is too long.", code="invalid_question"))
        settings = self._agent_settings
        if not settings.configured:
            return Err(
                SdkError(
                    "Configure the plugin-owned research model in the Hosted UI before consulting it.",
                    code="agent_not_configured",
                )
            )
        context = _ctx
        role = context.get("lanlan_name") if isinstance(context, Mapping) else None
        if not isinstance(role, str) or not role.strip():
            role = getattr(getattr(self, "_host_ctx", None), "current_lanlan", None)
        game = getattr(self._bridge, "session", None)

        async def standalone(reason):
            value = await self._agent_runtime.run(
                normalized,
                settings,
                capabilities=(make_history_tool(report_store=self._reports, question=normalized),),
            )
            value["continuity"] = {"bound": False, "reason": reason}
            return value

        if not isinstance(role, str) or not role.strip():
            result = await standalone("caller_identity_unavailable")
        else:
            service = getattr(self, "_workspace_service", None)
            if service is None or service.closed:
                service = WorkspaceService(
                    self.data_path("workspaces") / "work.sqlite",
                    self._agent_runtime,
                    game_session=lambda: getattr(self._bridge, "session", None),
                    report_store=self._reports,
                )
                self._workspace_service = service
            try:
                result = await service.consult(
                    role.strip(), game.colony_id if game is not None else None, normalized, settings
                )
            except NoCurrentWork:
                result = await standalone("no_retained_work")
            except (OSError, sqlite3.Error, ValueError) as exception:
                incident = self._record_unexpected("workspace", "Persistent work could not be completed.", exception)
                return Err(
                    SdkError(
                        "The ongoing work could not be saved or loaded; retry after checking storage.",
                        code="workspace_unavailable",
                        details={"incident_id": incident},
                    )
                )
        if result.get("report", {}).get("saved"):
            result["report"]["export"] = await self._export_report(result["report"]["report_id"], _ctx)
        handoff = _consultation_handoff(result, normalized)
        workspace = result.get("workspace")
        if workspace is not None:
            payload = {
                "summary": handoff["summary"],
                "delivery": handoff["delivery"],
                "history_saved": workspace["history_saved"],
                "report": result.get("report"),
                "prepared_at": utc_now(),
                "meaning": "Prepared host response; not a receipt of N.E.K.O consumption.",
            }
            try:
                await service.storage.call(
                    "record_delivery", workspace["workspace_id"], role.strip(), workspace["turn_id"], payload
                )
                service.revision += 1
            except (OSError, sqlite3.Error, RuntimeError):
                handoff["workspace"]["handoff_saved"] = False
        return Ok(handoff)

    async def _load_agent_tools(self) -> ToolSetup:
        setup = await self._catalog_tools.load()
        # Import runs outside this consultation. A ready database version is
        # pinned immediately; a first import never consumes the model deadline.
        self._knowledge.request_refresh()
        setup = compose_wiki_tools(setup, self._knowledge)
        setup = await compose_implementation_knowledge(setup)
        return setup

    async def _save_report(self, record: Mapping[str, Any]) -> Mapping[str, Any]:
        return await asyncio.to_thread(self._reports.save, record)

    async def _export_report(self, report_id: str, context: object) -> dict[str, Any]:
        run_id = context.get("run_id") if isinstance(context, Mapping) else None
        if not run_id:
            return {"status": "unavailable", "reason": "no_host_run"}
        try:
            record = await asyncio.to_thread(self._reports.read, report_id)
            outcome = await asyncio.wait_for(
                self.export_push(
                    export_type="text",
                    run_id=run_id,
                    text=ReportStore.markdown(record),
                    label="RimWorld report " + report_id,
                    description="Full historical report (" + record["state"] + ")",
                    mime="text/markdown",
                    delivery="silent",
                    timeout=2.0,
                ),
                timeout=2.5,
            )
            if isinstance(outcome, Mapping) and outcome.get("success") is False:
                raise RuntimeError("export rejected")
            return {"status": "exported"}
        except Exception:
            return {"status": "unavailable", "reason": "export_failed", "report_retained": True}

    @ui.action(id="rimworld_report", label="Read saved reports", refresh_context=False)
    @plugin_entry(
        id="rimworld_report",
        name="Retrieve RimWorld reports",
        description="Hosted UI access to saved historical reports; list or read one report and export its full artifact.",
        input_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "read"]},
                "report_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                "offset": {"type": "integer", "minimum": 0, "maximum": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        llm_result_fields=["summary"],
        timeout=8.0,
        metadata={"agent_auto": False, "llm_result_fields": ["summary"]},
    )
    async def rimworld_report(
        self, action: str = "list", report_id: str = "", offset: int = 0, limit: int = 5, **kwargs: object
    ):
        if (
            not isinstance(action, str)
            or action not in {"list", "read"}
            or type(offset) is not int
            or not 0 <= offset <= 1000
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            return Err(SdkError("Invalid report action or page.", code="invalid_report_request"))
        try:
            if action == "read":
                record = await asyncio.to_thread(self._reports.read, report_id)
                exported = await self._export_report(report_id, kwargs.get("_ctx"))
                summary = f"Historical {record['state']} report {report_id} is in the panel."
                summary += (
                    " Full artifact exported." if exported["status"] == "exported" else " Host export unavailable."
                )
                return Ok({"summary": summary, "report": record, "export": exported})
            result = await asyncio.to_thread(self._reports.list, offset, limit)
            summary = f"{result['total']} saved historical reports. IDs on this page:"
            included = 0
            for row in result["reports"]:
                title = str(row.get("question") or "").replace("\n", " ")[:50]
                line = f"\n{row['report_id']} ({row['state']}, {str(row['created_at'])[:19]}): {title}"
                continuation = f"\nContinue list at offset {offset + included + 1}."
                if count_handoff_tokens(summary + line + continuation)[0] > 180:
                    # Keep exact IDs discoverable even without a tokenizer or with a long title.
                    line = f"\n{row['report_id']} ({row['state']})"
                    if count_handoff_tokens(summary + line + continuation)[0] > 180:
                        break
                summary += line
                included += 1
            if offset + included < result["total"]:
                summary += f"\nContinue list at offset {offset + included}."
            result["summary"] = summary
            return Ok(result)
        except (ReportError, OSError, ValueError) as error:
            return Err(
                SdkError(
                    "The saved report could not be read; it may have expired or been removed.",
                    code=getattr(error, "code", "report_read_failed"),
                )
            )

    @ui.action(
        id="probe_game_overview",
        label="Probe game overview",
        tone="primary",
        refresh_context=True,
    )
    @plugin_entry(
        id="probe_game_overview",
        name="Probe RimWorld game overview",
        description="Request the colony overview section from the authenticated local RimWorld session.",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["summary"],
        timeout=5.0,
        metadata={"agent_auto": False},
    )
    async def probe_game_overview(self, **_: object):
        bridge = self._bridge
        if bridge is None:
            return Err(SdkError("RimWorld bridge is not listening.", code="bridge_not_running"))
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(
                    bridge.request,
                    "template_query",
                    {"requests": [{"target": {"type": "colony"}, "sections": ["overview"]}]},
                    2.5,
                ),
                timeout=3.5,
            )
            result = _query_summary(payload)
            if result["status"] not in {"ok", "partial"}:
                return Err(
                    SdkError(
                        result["summary"],
                        code=str(result.get("error_code") or "rimworld_query_failed"),
                        query_status=result["status"],
                    )
                )
            return Ok(result)
        except BridgeNotConnected:
            return Err(SdkError("No authenticated RimWorld game session is available.", code="game_offline"))
        except BridgeQueryTimeout as exception:
            return Err(
                SdkError(
                    "RimWorld did not answer the overview probe in time.",
                    code="query_timeout",
                    incident_id=exception.incident_id,
                )
            )
        except BridgeBusyError:
            return Err(SdkError("The RimWorld bridge is busy; retry later.", code="bridge_busy"))
        except BridgeUnsupportedError:
            return Err(
                SdkError(
                    "The connected RimWorld Mod does not support the game query capability.",
                    code="unsupported",
                )
            )
        except BridgeProtocolError as exception:
            return Err(
                SdkError(
                    "RimWorld returned an invalid or failed query response.",
                    code="protocol_error",
                    protocol_code=exception.protocol_code,
                    incident_id=exception.incident_id,
                )
            )
        except BridgeError as exception:
            return Err(SdkError("The RimWorld bridge request failed.", code=exception.code))
        except Exception:
            incident = self._state_store.record_incident(
                "probe_internal",
                "The colony overview probe failed unexpectedly.",
                level="error",
            )
            self.logger.exception("[probe] unexpected failure incident_id={}", incident)
            return Err(
                SdkError(
                    "The colony overview probe failed unexpectedly.",
                    code="bridge_internal",
                    incident_id=incident,
                )
            )

    async def _agent_query_tool(self, arguments: dict[str, Any]) -> Observation:
        operation = arguments.get("operation")
        query_arguments = arguments.get("arguments")
        if not isinstance(operation, str) or not operation.strip() or len(operation) > 128:
            raise ToolExecutionError("invalid_arguments", "operation must be non-empty text, at most 128 characters.")
        if not isinstance(query_arguments, dict):
            raise ToolExecutionError("invalid_arguments", "arguments must be a JSON object.")
        bridge = self._bridge
        if bridge is None:
            raise ToolExecutionError("bridge_not_running", "The RimWorld bridge is not listening.")
        # Catalog initialization can also be expensive on the first modded
        # game query; do not remove every game tool after a 2.5-second warmup.
        response_timeout = 9.0 if operation in FRAME_READ_OPERATIONS or operation in {"query", "query_catalog"} else 2.5
        try:
            payload = await asyncio.wait_for(
                asyncio.to_thread(bridge.request, operation, query_arguments, response_timeout),
                timeout=response_timeout + 1.0,
            )
        except BridgeNotConnected as exception:
            raise ToolExecutionError(
                "game_offline", "No authenticated active RimWorld session is available."
            ) from exception
        except BridgeQueryTimeout as exception:
            raise ToolExecutionError(
                "query_timeout", "RimWorld did not answer the colony query in time."
            ) from exception
        except BridgeBusyError as exception:
            raise ToolExecutionError("bridge_busy", "The RimWorld bridge is busy; retry later.") from exception
        except BridgeUnsupportedError as exception:
            raise ToolExecutionError(
                "unsupported", "The connected Mod does not support generic queries."
            ) from exception
        except BridgeProtocolError as exception:
            raise ToolExecutionError("protocol_error", "RimWorld returned an invalid colony response.") from exception
        except BridgeError as exception:
            raise ToolExecutionError(exception.code, "The RimWorld bridge request failed.") from exception
        except Exception as exception:
            incident = self._record_unexpected(
                "agent_query_tool",
                "The agent colony tool failed unexpectedly.",
                exception,
            )
            raise ToolExecutionError(
                "agent_tool_internal",
                f"The colony tool failed unexpectedly; incident {incident}.",
            ) from None

        return Observation.from_response(operation, query_arguments, payload)

    def _valid_settings_ui_nonce(self, candidate: object) -> bool:
        if not isinstance(candidate, str):
            return False
        try:
            return secrets.compare_digest(candidate, self._settings_action_nonce)
        except TypeError:
            return False

    async def _restore_previous_settings(
        self,
        settings: BridgeSettings,
        *,
        candidate_status: str,
    ) -> tuple[bool, dict[str, Any]]:
        """Restore the persisted bridge state after a pre-commit failure."""

        if candidate_status == "stop_failed":
            return False, {"status": "candidate_listener_retained"}
        restoration = await self._start_configured_bridge(settings, source="settings_rollback")
        restored = restoration["status"] in {"listening", "waiting_for_configuration"}
        if restored:
            self._state_store.diagnostics.record(
                "warning",
                "configuration",
                "The previous bridge state was restored after a settings failure.",
            )
            self._publish_coarse_status()
        return restored, restoration

    async def _start_configured_bridge(
        self,
        settings: BridgeSettings,
        *,
        source: str,
    ) -> dict[str, Any]:
        """Apply validated settings after the previous listener has stopped."""

        self._settings = settings
        self._state_store.transition(
            BridgeStatus.STARTING,
            endpoint=f"127.0.0.1:{settings.port}",
            port=settings.port,
            runtime={"phase": "starting", "worker_alive": False, "started_at": utc_now()},
            configuration={"token_configured": settings.token_configured},
        )
        if not settings.token_configured:
            self._state_store.diagnostics.record(
                "warning",
                "configuration",
                "Configure a 32-128 character pairing token before starting the listener.",
            )
            self._state_store.transition(
                BridgeStatus.STOPPED,
                runtime={"phase": "waiting_for_configuration", "worker_alive": False},
            )
            self._publish_coarse_status()
            return {"status": "waiting_for_configuration", "port": settings.port}

        try:
            bridge = BridgeServer(
                settings.pairing_token,
                self._state_store,
                self.get_logger("bridge"),
                port=settings.port,
                report_status=self._report_bridge_status,
            )
            self._bridge = bridge
            host, port = bridge.start()
        except BridgeStartError as exception:
            stopped = await self._stop_existing_bridge()
            self.logger.error(
                "[{}] bridge start failed code={} incident_id={}",
                source,
                exception.code,
                exception.incident_id,
            )
            self._state_store.transition(
                BridgeStatus.ERROR,
                runtime={"phase": "degraded" if stopped else "stop_failed", "worker_alive": not stopped},
            )
            self._publish_coarse_status()
            return {
                "status": "degraded" if stopped else "stop_failed",
                "port": settings.port,
                "incident_id": exception.incident_id,
            }
        except Exception as exception:
            incident = self._record_unexpected(
                f"{source}_bridge_start",
                "The bridge failed while applying settings.",
                exception,
            )
            stopped = await self._stop_existing_bridge()
            self._state_store.transition(
                BridgeStatus.ERROR,
                runtime={"phase": "error" if stopped else "stop_failed", "worker_alive": not stopped},
            )
            self._publish_coarse_status()
            return {
                "status": "degraded" if stopped else "stop_failed",
                "port": settings.port,
                "incident_id": incident,
            }

        self._state_store.update(
            runtime={"phase": "running", "worker_alive": True, "started_at": utc_now()},
            endpoint=f"{host}:{port}",
        )
        self._state_store.diagnostics.record("info", "lifecycle", "N.E.K.O RimWorld plugin started.")
        self._publish_coarse_status()
        return {"status": "listening", "host": host, "port": port}

    def _record_unexpected(self, scope: str, message: str, exception: Exception) -> str:
        """Record an unexpected failure without allowing credential text into logs."""

        incident = self._state_store.record_incident(scope, message, level="error")
        error_type = type(exception).__name__
        # Some configuration transports include request values in exception text.
        # Raise a new credential-free exception so logger.exception retains a
        # traceback without serializing the original exception or its message.
        try:
            raise RuntimeError(f"{scope} failed with {error_type}; details were redacted.") from None
        except RuntimeError:
            self.logger.exception(
                "[{}] unexpected failure incident_id={} error_type={}",
                scope,
                incident,
                error_type,
            )
        return incident

    async def _stop_existing_bridge(self) -> bool:
        bridge = self._bridge
        if bridge is None:
            return True
        try:
            stopped = bool(await asyncio.to_thread(bridge.stop, 3.0))
            if stopped and self._bridge is bridge:
                self._bridge = None
            return stopped
        except Exception as exception:
            self._record_unexpected(
                "shutdown_internal",
                "The bridge raised while stopping.",
                exception,
            )
            return False

    def _report_bridge_status(self, status: dict[str, Any]) -> None:
        try:
            self.report_status(json_safe_copy(status))
        except Exception:
            self.logger.warning("[status] bridge status update failed")

    def _publish_coarse_status(self) -> None:
        self._report_bridge_status(self._state_store.coarse_status())
