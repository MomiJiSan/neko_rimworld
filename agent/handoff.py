"""Transport-only validation for text handed from the private agent to N.E.K.O."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..state import redact_text
from .prompt import PLAIN_ANSWER_GUIDANCE, PLAIN_DETAIL_GUIDANCE, PLAIN_SUBMISSION_GUIDANCE
from .tokenization import tokenizer as _tokenizer

# The 180-token budget bounds the *model-authored* conclusion. Program-rendered
# fact references travel in the same handoff but are not the model's wording, so
# they are bounded separately. Charging both to the same 180 is what rejected
# 7 of 7 reference submissions as handoff_over_budget in the v1.3 fixed-observation
# run; every submission that used a reference failed, every one that avoided it
# was delivered.
HANDOFF_TOKEN_BUDGET = 180
# The unchanged host result parser truncates the entire summary at 200 tokens.
# Fact/model accounting never grants additional space on that transport. Keep
# this conservative contract covered by tests against the real host parser.
HANDOFF_TRANSPORT_TOKEN_BUDGET = 200
# Facts are bounded by *path count*, because that is the unit the model chooses.
# The older 1200-token fact ceiling remains a renderer safety guard, NOT an
# allowance to exceed the host's 200-token total. The pilot measured citations
# rendering 643, 915, 1213, 2800 and 3500 tokens; those cannot travel intact
# through the unchanged host and must be repaired before delivery.
#   * paths are counted per observation across ALL parts (including interleaved
#     ones); the largest historical single-observation citation was 10 paths,
#     so 16 leaves structural headroom but does not guarantee transport fit
#   * 4 observations are allowed, so the total cap must be BELOW 4 x the per-reference
#     cap or it can never bind; 32 is half of 4 x 16 and ~1200 tokens worst case
MAX_PATHS_PER_REFERENCE = 16
MAX_TOTAL_REFERENCE_PATHS = 32
HANDOFF_FACTS_TOKEN_BUDGET = 1_200
MAX_FULL_TEXT_CHARS = 12_000
MAX_AGENT_TEXT_CHARS = 4_000
_NUMERAL = re.compile(r"\d+(?:\.\d+)?")

_MISSING_HANDOFF = (
    "The internal RimWorld agent completed without a callback-safe response. "
    "Review the full result in the plugin panel."
)


class HandoffValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def count_handoff_tokens(text: str) -> tuple[int, str]:
    encoding = _tokenizer()
    if encoding is not None:
        return len(encoding.encode(text)), "o200k_base"
    return len(text.encode("utf-8", "replace")), "utf8_bytes_upper_bound"


def _validated_text(value: object, *, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandoffValidationError("invalid_submission", f"{field} must be a non-empty string.")
    raw = value.replace("\x00", " ").strip()
    if len(raw) > max_chars:
        raise HandoffValidationError(
            "submission_too_large",
            f"{field} exceeded its {max_chars}-character transport safety limit.",
        )
    return redact_text(raw, max(len(raw) + 1, 1))


def validate_full_text(value: object) -> str:
    return _validated_text(value, field="full_text", max_chars=MAX_FULL_TEXT_CHARS)


def validate_agent_text(value: object, *, fact_tokens: int = 0) -> str:
    """Bound the handoff. Model-written text and rendered facts have separate budgets.

    `fact_tokens` is the share of the text that the program rendered from cited data.
    It is passed explicitly, never inferred: a caller that does not pass it means the
    text is entirely model-written. Inferring it from absence was ambiguous — the last
    mile could not tell "the model wrote too much" from "the facts are large", and
    loosening the ceiling to accommodate facts silently accepted over-long model prose.
    Independent of that accounting, the combined text must fit the unchanged host.
    """
    text = _validated_text(value, field="agent_text", max_chars=MAX_AGENT_TEXT_CHARS)
    tokens, _ = count_handoff_tokens(text)
    if fact_tokens > HANDOFF_FACTS_TOKEN_BUDGET:
        raise HandoffValidationError(
            "facts_over_budget",
            f"The rendered fact references use {fact_tokens} tokens; the renderer safety maximum is "
            f"{HANDOFF_FACTS_TOKEN_BUDGET}. Cite fewer paths.",
        )
    text_tokens = max(0, tokens - fact_tokens)
    if text_tokens > HANDOFF_TOKEN_BUDGET:
        raise HandoffValidationError(
            "handoff_over_budget",
            f"agent_text uses {text_tokens} tokens of model-written text "
            f"({tokens} total, of which {fact_tokens} are rendered facts); "
            f"the model text maximum is {HANDOFF_TOKEN_BUDGET}.",
        )
    if tokens > HANDOFF_TRANSPORT_TOKEN_BUDGET:
        raise HandoffValidationError(
            "handoff_transport_over_budget",
            f"The rendered answer uses {tokens} tokens in total "
            f"({fact_tokens} rendered facts and {text_tokens} model-written); "
            f"the host transport maximum is {HANDOFF_TRANSPORT_TOKEN_BUDGET}. "
            "Cite fewer paths or shorten the conclusion; keep the complete report in detail "
            "and preserve scope and unknowns. No new game query is needed for this repair.",
        )
    return text


class SubmissionPatchError(HandoffValidationError):
    def __init__(self, code: str, message: str, details: dict[str, Any]):
        super().__init__(code, message)
        self.details = details


def _answer_parts(value, *, references_supported=True):
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise HandoffValidationError(
            "invalid_submission", "answer requires 1..16 ordered parts matching the submission schema."
        )
    normalized = []
    for index, part in enumerate(value):
        if not isinstance(part, dict):
            raise HandoffValidationError("invalid_submission", f"answer[{index}] must be an object.")
        if not references_supported and set(part) != {"text"}:
            extra = sorted(set(part) - {"text"})
            top_level = sorted(set(extra) & {"answer", "detail", "language"})
            message = f"answer[{index}] accepts only {{text}}."
            if extra:
                message += " Unsupported fields: " + ", ".join(extra) + "."
            if top_level:
                message += (
                    " Put " + ", ".join(top_level) + " at the top level of submit_response, not inside an answer part."
                )
            raise HandoffValidationError(
                "unsupported_reference" if "observation_id" in part else "invalid_submission", message
            )
        if set(part) == {"text"}:
            normalized.append(
                {"text": _validated_text(part["text"], field="answer.text", max_chars=MAX_FULL_TEXT_CHARS)}
            )
        elif "observation_id" in part and not set(part) - {"observation_id", "paths"}:
            if not isinstance(part["observation_id"], str):
                raise HandoffValidationError(
                    "invalid_submission", "observation_id must identify this run's observation."
                )
            normalized.append(copy.deepcopy(part))
        else:
            raise HandoffValidationError(
                "invalid_submission",
                "A part is either {text} or {observation_id, paths?}; no added labels or expressions.",
            )
    return normalized


def _distinctive_numerals(text: str) -> set[str]:
    """Numbers a reader would take as a reported measurement.

    Single digits are excluded on purpose: they occur in ordinary prose
    ("3 名殖民者") and refusing those would reject correct answers. Decimals and
    multi-digit numbers are exactly the class the model mistypes when it retypes
    a reading, so those are what the check compares.
    """
    found: set[str] = set()
    for token in _NUMERAL.findall(text or ""):
        if "." in token:
            normalized = token.rstrip("0").rstrip(".")
            if normalized and normalized != "0":
                found.add(normalized)
        elif len(token.lstrip("0")) >= 2:
            found.add(token)
    return found


def restated_facts(text: str, rendered_facts: str) -> list[str]:
    """Model-written numbers that a referenced reading already reports.

    Mechanical on purpose. The reference submission protocol tells the model not
    to restate referenced values; in the v1.3 live run it restated them anyway,
    and the duplicate copy is what pushed the handoff over budget.
    """
    facts = _distinctive_numerals(rendered_facts)
    if not facts:
        return []
    return sorted(_distinctive_numerals(text) & facts)


def _first_index(path: str) -> str:
    return re.sub(r"/~2(?=/|$)", "/0", path or "")


def referenced_numbers(parts, observations) -> set[str]:
    """The numbers the model actually cited, resolved from the referenced paths.

    Deliberately **not** the whole rendered handoff: that string also carries the
    display window and sample tick, and those numbers appear legitimately in prose
    ("共 64 个部位，已列出 20 个" is a correct completeness disclosure). Comparing
    against the cited values only keeps the check on the readings themselves.
    """
    from .presentation import _get

    by_id: dict[str, Any] = {}
    for item in observations or ():
        if isinstance(item, Mapping) and isinstance(item.get("observation_id"), str):
            by_id[item["observation_id"]] = item
    found: set[str] = set()
    for part in parts:
        observation = by_id.get(part.get("observation_id")) if isinstance(part, Mapping) else None
        if observation is None:
            continue
        payload = (observation.get("response") or {}).get("payload") or {}
        data = payload.get("data")
        for path in part.get("brief_paths") or part.get("paths") or [""]:
            try:
                value = _get(data, _first_index(path))
            except Exception:  # noqa: BLE001 - an unresolvable path is reported by the renderer
                continue
            try:
                encoded = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                continue
            found |= _distinctive_numerals(encoded)
    return found


def restated_values(text: str, values: Iterable[str]) -> list[str]:
    wanted = set(values)
    if not wanted:
        return []
    return sorted(_distinctive_numerals(text) & wanted)


def rendered_authored_text(text: str,language: str) -> str:
    """Remove only a trailing, matching tool-parameter serialization fragment.

    Original submission parts remain unchanged. Native evidence/reference text
    and embedded quoted markup are never routed through this cleanup.
    """
    return re.sub(r'</answer>\s*<parameter\s+name=[\"\x27]language[\"\x27]>\s*'+re.escape(language)+r'\s*(?:</parameter>)?\s*$', '',text).rstrip()


def _render_answer(parts, observations, language):
    from .presentation import EvidenceError, render_evidence

    grouped = []
    reference_paths: dict[str, set[str]] = {}
    for part in parts:
        if "text" in part:
            grouped.append(part)
            continue
        identity = part["observation_id"]
        paths = part.get("paths", [""])
        if (
            not isinstance(paths, list)
            or not 1 <= len(paths) <= MAX_PATHS_PER_REFERENCE
            or any(not isinstance(path, str) for path in paths)
        ):
            raise EvidenceError(f"paths requires 1..{MAX_PATHS_PER_REFERENCE} data-relative JSON Pointers.")
        # Count the same observation across ALL parts, even when text or another
        # observation separates them. Normalize like the renderer so /a and a
        # are the same path. Keep rendering order independent of this limit.
        paths = list(dict.fromkeys(path if not path or path.startswith("/") else "/" + path for path in paths))
        reference_paths.setdefault(identity, set()).update(paths)
        if grouped and grouped[-1].get("observation_id") == identity:
            grouped[-1]["brief_paths"] = list(dict.fromkeys(grouped[-1]["brief_paths"] + paths))
        else:
            grouped.append({"observation_id": identity, "brief_paths": paths[:]})
    if len(reference_paths) > 4:
        raise EvidenceError("An answer can reference at most 4 distinct observations.")
    total_paths = sum(len(paths) for paths in reference_paths.values())
    for paths in reference_paths.values():
        if len(paths) > MAX_PATHS_PER_REFERENCE:
            raise EvidenceError(
                f"One observation cites {len(paths)} paths across answer parts; "
                f"the maximum is {MAX_PATHS_PER_REFERENCE}. "
                "Cite the readings this conclusion depends on and put the rest in detail."
            )
    if total_paths > MAX_TOTAL_REFERENCE_PATHS:
        raise EvidenceError(
            f"This answer cites {total_paths} paths in total; the maximum is {MAX_TOTAL_REFERENCE_PATHS}. "
            "Cite the readings this conclusion depends on and put the rest in detail."
        )
    full, short, sources = [], [], []
    fact_tokens = 0
    seen_readings = set()
    written: list[str] = []
    rendered: list[str] = []
    cited_parts: list[dict] = []
    for part in grouped:
        if "text" in part:
            text=rendered_authored_text(part["text"],language)
            full.append(text)
            short.append(text)
            written.append(text)
        else:
            cited_parts.append(part)
            body, brief, provenance = render_evidence([part], observations, language, _seen=seen_readings)
            if body:
                full.append(body)
            if brief:
                short.append(brief)
                rendered.append(brief)
            sources.extend(provenance["sources"])
            fact_tokens += count_handoff_tokens(brief)[0]
    if rendered:
        overlap = restated_values("\n".join(written), referenced_numbers(cited_parts, observations))
        if overlap:
            raise EvidenceError(
                "The referenced reading already reports "
                + ", ".join(overlap)
                + ". Do not restate referenced values in a text part; state what they mean instead."
            )
    return (
        "\n\n".join(full),
        "\n".join(short),
        {
            "format": "answer_parts.v1",
            "renderer_version": "observed_values.v1",
            "sources": sources,
        },
        fact_tokens,
    )


@dataclass(slots=True)
class SubmissionDraft:
    answer: list[dict[str, Any]] = field(default_factory=list)
    detail: str | None = None
    language: str = "zh"
    full_text: str | None = None
    agent_text: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    revision: int = 0
    errors: dict[str, dict[str, str]] = field(default_factory=dict)
    # Set when an otherwise well-formed submission was refused for an unsupported
    # claim. The refused text is never committed, so this only tells the degraded
    # paths that the last thing the model wrote is not deliverable.
    claims_blocked: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "answer": copy.deepcopy(self.answer),
            "detail": self.detail,
            "language": self.language,
            "full_text": self.full_text,
            "agent_text": self.agent_text,
            "provenance": copy.deepcopy(self.provenance),
            "revision": self.revision,
            "saved_fields": [key for key in ("answer", "detail") if getattr(self, key)],
            "errors": copy.deepcopy(self.errors),
            "complete": bool(self.full_text and self.agent_text and not self.errors),
        }

    def apply(
        self, patch: Mapping[str, Any], *, observations=(), references_supported=True, claims_supported=True
    ) -> dict[str, Any]:
        from .presentation import EvidenceError

        if not patch or set(patch) - {"answer", "detail", "language"}:
            raise SubmissionPatchError(
                "invalid_submission",
                "Submit answer parts, optional detail and language.",
                {"saved_fields": self.snapshot()["saved_fields"]},
            )
        previous_answer = self.answer
        changed = set()
        for key in ("language", "answer", "detail"):
            if key not in patch:
                continue
            try:
                value = patch[key]
                if key == "language":
                    if value not in {"zh", "en"}:
                        raise HandoffValidationError("invalid_submission", "language must be zh or en.")
                elif key == "answer":
                    value = _answer_parts(value, references_supported=references_supported)
                    _render_answer(value, observations, self.language)
                elif value is not None:
                    value = _validated_text(value, field=key, max_chars=MAX_FULL_TEXT_CHARS)
                if value != getattr(self, key):
                    changed.add(key)
                setattr(self, key, value)
                self.errors.pop(key, None)
            except (HandoffValidationError, EvidenceError, TypeError) as error:
                self.errors[key] = {"code": getattr(error, "code", "invalid_evidence"), "message": str(error)}
        # Text edits retain the saved report; checking prose consistency belongs
        # to the author/review. Only a changed explicit evidence dependency needs
        # reconfirmation, rather than treating every text edit as a new conclusion.
        same_references = [p for p in previous_answer if "text" not in p] == [p for p in self.answer if "text" not in p]
        if (
            "answer" in changed
            and previous_answer
            and self.detail is not None
            and "detail" not in patch
            and not same_references
        ):
            self.errors["detail"] = {
                "code": "submission_requires_confirmation",
                "message": "Referenced evidence changed; update or reconfirm the matching detail.",
            }
        if changed:
            self.revision += 1
        self.errors.pop("delivery", None)
        # The deterministic claim check runs BEFORE any text is committed. It used
        # to run last, which left a refused sentence in full_text/agent_text and let
        # the timeout/cancel/degraded paths deliver it through fallback_answer.
        # Field-level errors keep precedence so the model repairs structure first.
        if claims_supported and not self.errors:
            from .claims import check_claims, describe

            rejected = check_claims(self.answer, self.detail, observations=observations)
            if rejected:
                self.claims_blocked = True
                first = rejected[0]
                raise SubmissionPatchError(
                    first["code"],
                    "Unsupported claim needs evidence from this run: " + describe(rejected),
                    {
                        "saved_fields": self.snapshot()["saved_fields"],
                        "rejected_claims": rejected,
                        "required_fields": list(dict.fromkeys(item.get("field", "answer") for item in rejected)),
                    },
                )
            # The current saved draft passed the claim check. A later length
            # rejection must not inherit the previous draft's claim refusal.
            self.claims_blocked = False
        self.full_text = self.agent_text = None
        fact_tokens = 0
        short = ""
        try:
            valid_answer = self.answer if "answer" not in self.errors else []
            full, short, provenance, fact_tokens = _render_answer(valid_answer, observations, self.language)
            self.provenance = provenance
            valid_detail = self.detail if "detail" not in self.errors and "answer" not in self.errors else None
            if valid_detail and valid_detail != full:
                full = "\n\n".join(part for part in (full, valid_detail) if part)
            if full:
                self.full_text = validate_full_text(full)
            if not short:
                raise HandoffValidationError(
                    "invalid_submission",
                    "Provide answer parts for the short handoff; detail cannot replace them.",
                )
            self.agent_text = validate_agent_text(short, fact_tokens=fact_tokens)
        except (HandoffValidationError, EvidenceError) as error:
            self.errors["delivery"] = {"code": getattr(error, "code", "invalid_evidence"), "message": str(error)}
        if self.errors:
            required = [key for key in self.errors if key != "delivery"]
            if "delivery" in self.errors:
                required.append("answer")
            details = {
                "saved_fields": self.snapshot()["saved_fields"],
                "required_fields": list(dict.fromkeys(required)),
                "field_errors": copy.deepcopy(self.errors),
                "draft_revision": self.revision,
            }
            if references_supported:
                handoff_total = count_handoff_tokens(short)[0] if short else 0
                details.update(
                    fact_tokens=fact_tokens,
                    fact_token_budget=HANDOFF_FACTS_TOKEN_BUDGET,
                    handoff_tokens=handoff_total,
                    transport_token_budget=HANDOFF_TRANSPORT_TOKEN_BUDGET,
                    text_tokens_used=max(0, handoff_total - fact_tokens),
                    text_token_budget=HANDOFF_TOKEN_BUDGET,
                )
            else:
                details.update(
                    handoff_tokens=count_handoff_tokens(short)[0] if short else None, token_budget=HANDOFF_TOKEN_BUDGET
                )
            first = next(iter(self.errors.values()))
            repair = " Valid inputs are saved. Correct the required fields."
            if references_supported and any("observation_id" in part for part in self.answer):
                repair += " Reference parts already emit the facts; do not restate them in text parts."
            elif self.errors.get("delivery", {}).get("code") == "facts_over_budget":
                repair += " Reference fewer paths in each fact-reference part."
            elif self.errors.get("delivery", {}).get("code") == "handoff_over_budget":
                repair += (
                    " Shorten answer to the concise conclusion with room below the token limit;"
                    " keep requested facts in detail and preserve the conclusion's scope and unknowns."
                    " No new game query is needed for this length repair."
                )
            if "answer" in required and self.detail is not None:
                repair += (
                    " Omitted detail remains saved; update it only when the report changes;"
                    " do not clear detail that is needed to answer the question."
                )
            raise SubmissionPatchError(
                first["code"],
                " ".join(item["message"] for item in self.errors.values()) + repair,
                details,
            )
        result = validate_submission(self.full_text, self.agent_text, fact_tokens=fact_tokens)
        # The claim check already ran, before this text was committed. Reaching here
        # means the committed text is accepted, so any earlier block is lifted.
        self.claims_blocked = False
        result["provenance"] = copy.deepcopy(self.provenance)
        return result


def make_submission_tool(
    draft: SubmissionDraft, observations, on_update=None, *, references_supported=True, claims_supported=True
):
    """One production terminal implementation for the runtime and fixed-observation studies."""
    from .tool_contracts import ToolExecutionError, ToolSpec

    async def submit(arguments):
        arguments = dict(arguments)
        if not references_supported:
            answer = arguments.get("answer")
            if isinstance(answer, str):
                arguments["answer"] = [{"text": answer}]
            elif isinstance(answer, dict) and set(answer) == {"text"}:
                # Unambiguous legacy shape, lossless normalization only. Token,
                # content and evidence checks still run through the same draft.
                arguments["answer"] = [answer]
        parts = arguments.get("answer")
        if (
            not references_supported
            and isinstance(parts, list)
            and any(isinstance(part, dict) and "observation_id" in part for part in parts)
        ):
            raise ToolExecutionError(
                "unsupported_reference", "This Mod lacks result descriptions; submit answer as a plain text string."
            )
        try:
            result = draft.apply(
                arguments,
                observations=observations,
                references_supported=references_supported,
                claims_supported=claims_supported,
            )
        except SubmissionPatchError as error:
            message = str(error)
            details = error.details
            if not references_supported:
                import copy

                def text_feedback(value):
                    return (
                        value.replace(
                            "Submit answer parts, optional detail and language.",
                            "Submit answer as text, optional detail and language.",
                        )
                        .replace("answer.text", "answer")
                        .replace(
                            "answer requires 1..16 ordered parts matching the submission schema.",
                            "answer must be a nonempty text string.",
                        )
                        .replace(
                            "Provide answer parts for the short handoff; detail cannot replace them.",
                            "Provide the short answer text; detail cannot replace it.",
                        )
                    )

                message = text_feedback(message)
                details = copy.deepcopy(details)
                for field_error in details.get("field_errors", {}).values():
                    if isinstance(field_error.get("message"), str):
                        field_error["message"] = text_feedback(field_error["message"])
            if on_update is not None:
                on_update(draft.snapshot(), "submission_patch_rejected", details)
            raise ToolExecutionError(error.code, message, details=details) from None
        if on_update is not None:
            on_update(
                draft.snapshot(),
                "submission_patch_accepted",
                {"saved_fields": draft.snapshot()["saved_fields"], "revision": draft.revision},
            )
        return result

    spec = ToolSpec(
        name="submit_response",
        description=(
            "Finish by composing the answer itself as ordered parts. A reference part EMITS the selected facts with their necessary subject, unit, scope and unknowns; it is not a citation attachment. "
            "For factual questions, reference parts alone are sufficient when they cover the requested facts. Add a text part only for additional interpretation, not a second restatement of the selected facts. "
            "Text-only answers use one {text:...} part. Optional detail is extra explanation for the full report. "
            "Model-written answer text must fit 180 tokens; rendered facts have a separate budget, "
            "and the complete short handoff must fit the host's 200-token transport limit. "
            "Full report plus detail must fit 12000 characters. "
            "Use actual data-relative paths, with or without a leading slash: do not prefix /payload/data or /response/payload/data; array positions are integers, never metadata ~2 templates. "
            "Value/status paths bound to the same reading are rendered once. Valid inputs survive rejection. Omitted detail is retained; update it when changing the report's conclusions, or clear it explicitly with null only if unnecessary. Submit alone after reading query results."
        ),
        parameters={
            "type": "object",
            "properties": {
                "answer": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 16,
                    "description": "The actual answer, shared by full report and short handoff. Do not append reference parts to a textual copy of the same facts.",
                    "items": {
                        "oneOf": [
                            {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string", "minLength": 1, "maxLength": MAX_FULL_TEXT_CHARS}
                                },
                                "required": ["text"],
                                "additionalProperties": False,
                            },
                            {
                                "type": "object",
                                "properties": {
                                    "observation_id": {"type": "string"},
                                    "paths": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": MAX_PATHS_PER_REFERENCE,
                                        "items": {"type": "string"},
                                        "description": "Existing paths relative to that observation's data (at most 16 for one observation; at most 32 total). These values will appear in the answer; no formulas, new filters or invented labels. Omit for the whole bounded result.",
                                    },
                                },
                                "required": ["observation_id"],
                                "additionalProperties": False,
                            },
                        ]
                    },
                },
                "detail": {
                    "type": ["string", "null"],
                    "maxLength": MAX_FULL_TEXT_CHARS,
                    "description": "Optional further explanation, excluded from the short handoff. null clears saved detail.",
                },
                "language": {
                    "type": "string",
                    "enum": ["zh", "en"],
                    "default": "zh",
                    "description": "Language of the whole submission. Set only here at the top level, never inside answer parts.",
                },
            },
            "minProperties": 1,
            "additionalProperties": False,
        },
        handler=submit,
        terminal=True,
    )
    if not references_supported:
        spec.parameters["properties"]["answer"] = {
            "type": "string",
            "minLength": 1,
            "description": PLAIN_ANSWER_GUIDANCE,
        }
        spec.parameters["properties"]["detail"]["description"] = PLAIN_DETAIL_GUIDANCE
        return ToolSpec(
            name=spec.name,
            description=PLAIN_SUBMISSION_GUIDANCE,
            parameters=spec.parameters,
            handler=submit,
            terminal=True,
        )
    return spec


def validate_submission(full_text: object, agent_text: object, *, fact_tokens: int = 0) -> dict[str, Any]:
    """Validate mechanics only; never interpret or rewrite domain content.

    `fact_tokens` is the program-rendered share of `agent_text`. Callers that render
    references pass it so the model text is charged separately; everyone else leaves
    the default and the whole handoff counts as model-written.
    """

    full = validate_full_text(full_text)
    handoff = validate_agent_text(agent_text, fact_tokens=fact_tokens)
    handoff_tokens, tokenizer = count_handoff_tokens(handoff)
    full_tokens, _ = count_handoff_tokens(full)
    return {
        "full_text": full,
        "agent_text": handoff,
        "delivery": {
            "format": "natural_text",
            "token_budget": HANDOFF_TOKEN_BUDGET,
            "fact_token_budget": HANDOFF_FACTS_TOKEN_BUDGET,
            "fact_tokens": fact_tokens,
            "handoff_tokens": handoff_tokens,
            "full_answer_tokens": full_tokens,
            "tokenizer": tokenizer,
            "transport_token_budget": HANDOFF_TRANSPORT_TOKEN_BUDGET,
            "host_truncation_expected": handoff_tokens > HANDOFF_TRANSPORT_TOKEN_BUDGET,
        },
    }


def attach_agent_handoff(result: object) -> dict[str, Any]:
    """Attach an already-authored handoff without deriving any game meaning."""

    source = dict(result) if isinstance(result, Mapping) else {}
    status = str(source.get("status") or "degraded").lower()
    answer = source.get("answer")
    agent_text = source.get("agent_text")
    submission_mode = str(source.get("submission_mode") or "unknown")
    try:
        if agent_text is None and status == "ok":
            agent_text = answer
            submission_mode = "text_fallback"
        # The rendered-fact share travels with the result, so the last mile can keep
        # charging model text and program text separately instead of guessing.
        delivery = source.get("delivery")
        fact_tokens = 0
        if isinstance(delivery, Mapping):
            try:
                fact_tokens = max(0, int(delivery.get("fact_tokens") or 0))
            except (TypeError, ValueError):
                fact_tokens = 0
        validated = validate_submission(answer, agent_text, fact_tokens=fact_tokens)
        text = validated["agent_text"]
        delivery = dict(validated["delivery"])
        delivery["submission_mode"] = submission_mode
    except HandoffValidationError as exception:
        # The report may exist, but a failed callback is not successful delivery.
        # Preserve cancelled/degraded states; never leave a new fallback as ok.
        if status == "ok":
            source["status"] = "degraded"
        text = _MISSING_HANDOFF
        handoff_tokens, tokenizer = count_handoff_tokens(text)
        full_tokens = count_handoff_tokens(answer)[0] if isinstance(answer, str) else None
        delivery = {
            "format": "natural_text",
            "token_budget": HANDOFF_TOKEN_BUDGET,
            "handoff_tokens": handoff_tokens,
            "full_answer_tokens": full_tokens,
            "tokenizer": tokenizer,
            "submission_mode": "operational_fallback",
            "host_truncation_expected": False,
            "error_code": exception.code,
        }
    source["summary"] = text
    source["reply"] = text
    source["delivery"] = delivery
    return source


__all__ = [
    "HANDOFF_FACTS_TOKEN_BUDGET",
    "MAX_PATHS_PER_REFERENCE",
    "MAX_TOTAL_REFERENCE_PATHS",
    "HANDOFF_TOKEN_BUDGET",
    "HANDOFF_TRANSPORT_TOKEN_BUDGET",
    "HandoffValidationError",
    "SubmissionDraft",
    "SubmissionPatchError",
    "validate_full_text",
    "validate_agent_text",
    "attach_agent_handoff",
    "count_handoff_tokens",
    "validate_submission",
]
