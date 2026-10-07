"""Compose negotiated game tools with caller-owned work capabilities."""

from __future__ import annotations

from typing import Any, Protocol

from .handoff import make_submission_tool
from .prompt import build_system_prompt
from .tool_contracts import ToolSetup, ToolSpec
from .tools import ToolRegistry


class ExecutionSurface(Protocol):
    """The existing surface contract, explicit for production and sealed studies."""

    supports_finalization: bool
    finalizing: bool

    def prompt(self, preferred_cycles: int) -> str: ...
    def tools(self) -> tuple[ToolSpec, ...]: ...
    def request_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def attach_result(self, result: dict[str, Any]) -> dict[str, Any]: ...
    def accept_text(self, text: str, finish_reason: Any) -> Any: ...
    def snapshot(self) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...


def compose_tools(game_tools, capabilities):
    result = tuple(game_tools) + tuple(capabilities)
    names = [tool.name for tool in result]
    if len(names) != len(set(names)):
        raise ValueError("Game tools and work capabilities must have distinct names")
    return result


class ExecutionEnvironment:
    def __init__(
        self,
        setup: ToolSetup,
        session,
        update,
        *,
        interaction_factory=None,
        reference_report=None,
        registry_factory=ToolRegistry,
    ):
        self.setup = setup
        self.session = session
        self.update = update
        self.registry_factory = registry_factory
        self.set_prompt(
            build_system_prompt(
                session.preferred_cycles,
                references_supported=setup.references_supported,
            )
            + ("\n\n" + setup.context if setup.context else "")
        )
        if interaction_factory is not None:
            session.interaction = interaction_factory(setup, session, update)
            if session.interaction is None:
                from .tool_contracts import ToolExecutionError

                raise ToolExecutionError("interaction_surface_unavailable", "The configured consultation surface is unavailable.", recoverable=False)
            self.set_prompt(session.interaction.prompt(session.preferred_cycles))
        if session.interaction is None and setup.references_supported:
            session.context_pruner = None

    def registry(self, *, surface=False, finalizing=False):
        session = self.session
        registry = self.registry_factory(
            context_pruner=session.context_pruner,
            context_enabled=session.context_pruner is not None,
        )
        if surface and session.interaction is not None:
            game = session.interaction.tools()
        elif finalizing:
            game = (self.submission(),)
        else:
            game = (*self.setup.tools, self.submission())
        work = () if finalizing else (*self.setup.capabilities, *session.capabilities)
        for spec in compose_tools(game, work):
            registry.register(spec)
        return registry

    def set_prompt(self, prompt):
        if self.session.workspace_id is not None:
            prompt = prompt.replace(
                "No long-term memory, game mutation or autonomous action.",
                "The caller retains a persistent colony workspace. No game mutation or autonomous action.",
            )
        self.session.messages[0]["content"] = prompt

    def submission(self):
        return make_submission_tool(
            self.session.submission,
            self.session.observations,
            self.update,
            references_supported=self.setup.references_supported,
        )
