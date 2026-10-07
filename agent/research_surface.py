"""Question policy and delivery, independent of the negotiated game vocabulary."""

from dataclasses import dataclass

from .handoff import attach_agent_handoff, make_submission_tool


@dataclass(frozen=True, slots=True)
class ResearchGuidance:
    read_guide: str
    native_state: bool
    activity_route: bool
    definition_guide: str = ""
    compatibility_guide: str = ""


class ReadOnlyResearchSurface:
    """One current research policy; subclasses supply only game reads/guidance."""

    supports_finalization = True
    references_supported = False

    def __init__(self, setup, session, update):
        self.setup = setup
        self.session = session
        self.update = update
        self.finalizing = False

    def prompt(self, preferred_cycles):
        guidance = self.guidance
        from .context_tools import query_guide, task_prompt

        prompt = task_prompt(
            preferred_cycles, finalizing=self.finalizing, references_supported=self.references_supported
        )
        if not self.finalizing:
            prompt += "\n\n" + query_guide(
                native_state=guidance.native_state, activity=guidance.activity_route
            )
            prompt += guidance.compatibility_guide
        return prompt

    def submission(self):
        return make_submission_tool(
            self.session.submission,
            self.session.observations,
            self.update,
            references_supported=self.references_supported,
        )

    def tools(self):
        submit = self.submission()
        return (submit,) if self.finalizing else (*self.read_tools(), submit)

    def request_messages(self, messages):
        return messages

    def accept_text(self, text, finish_reason):
        return None

    def attach_result(self, result):
        return attach_agent_handoff(result)

    async def aclose(self):
        return None
