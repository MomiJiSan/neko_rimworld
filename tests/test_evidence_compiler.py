import copy
import json

import pytest

from plugin.plugins.neko_rimworld.agent import stage_review
from plugin.plugins.neko_rimworld.agent.contracts import AssistantTurn, ToolCall, ToolResult
from plugin.plugins.neko_rimworld.agent.evidence_compiler import (
    FORMAT,
    EvidenceCompiler,
    EvidenceDecoder,
    canonical,
    is_compiled,
    model_size,
)
from plugin.plugins.neko_rimworld.agent.evidence_view import review_evidence
from plugin.plugins.neko_rimworld.agent.object_tools import ObjectReader
from plugin.plugins.neko_rimworld.agent.session import AgentRunSession
from plugin.plugins.neko_rimworld.agent.tools import MAX_TOOL_RESULT_CHARS, ToolRegistry, ToolSpec


def reading(tick=1, actor="A", amount=0):
    return {
        "ok": True,
        "tool": "look",
        "data": {
            "sample": {"load_epoch": "world-1", "game_tick": tick},
            "scope": {"actor": actor, "user": "patient", "population": "selected things"},
            "rows": [
                {
                    "id": f"item-{i}",
                    "definition": "UnknownModDefinition",
                    "value": amount,
                    "unit": "stack_units",
                    "unknown": None,
                    "complete": False,
                    "description": "Current observation, not a claim about ownership or action eligibility. " * 5,
                }
                for i in range(30)
            ],
            "issues": [{"path": ["rows", 2], "code": "partial"}],
        },
    }


def test_lossless_stream_keeps_samples_context_order_types_and_unknown_mod_fields():
    originals = [reading(), reading(2, "B", 3)]
    originals[0]["data"]["extension"] = {"$ref": "literal value", "$table": {"unknown": [None, False, 0, 0.0, "0"]}}
    originals[1]["data"]["extension"] = {"empty": [], "missing_is_not_null": {}, "text": "中文 / ~ 😀"}
    saved = copy.deepcopy(originals)
    compiler = EvidenceCompiler()
    decoder = EvidenceDecoder()
    views = [compiler.compile(v) for v in originals]
    assert all(v.encoded for v in views)
    for original, view in zip(originals, views):
        assert canonical(decoder.decode(json.loads(view.content))) == canonical(original)
    assert originals == saved
    # Later values may reuse exact definitions, but never replace actor/tick.
    assert views[1].view_chars < views[1].source_chars
    with pytest.raises(ValueError, match="unresolved"):
        EvidenceDecoder().decode(json.loads(views[1].content))


def test_small_and_unhelpful_views_do_not_commit_invisible_definitions():
    compiler = EvidenceCompiler()
    decoder = EvidenceDecoder()
    small = {"ok": True, "data": {"answer": None}}
    assert not compiler.compile(small).encoded
    unique = {"ok": True, "text": "unique text " * 110}
    first = compiler.compile(unique)
    assert not first.encoded
    second = compiler.compile({"rows": [unique] * 12})
    assert decoder.decode(json.loads(second.content)) == {"rows": [unique] * 12}


def test_tables_preserve_missing_fields_null_and_bool_number_distinction():
    original = {
        "rows": [{"a": False, "b": 0}, {"a": 0, "b": False}, {"a": None, "b": "0"}] * 200,
        "ragged": [{"x": None}, {}, {"x": False}],
    }
    view = EvidenceCompiler().compile(original)
    assert "$table" in view.content
    assert canonical(EvidenceDecoder().decode(json.loads(view.content))) == canonical(original)


@pytest.mark.parametrize("mutation", ["hash", "definition", "row_width", "reference_cycle"])
def test_invalid_compiled_inputs_are_rejected_without_decoder_state_commit(mutation):
    view = json.loads(EvidenceCompiler().compile(reading()).content)
    if mutation == "hash":
        view["source_sha256"] = "0" * 64
    elif mutation == "definition":
        view["shared"] = {}
    elif mutation == "row_width":
        view["value"] = {"$table": {"columns": ["a"], "rows": [[1, 2]]}}
    else:
        view["shared"]["cycle"] = {"$ref": "cycle"}
        view["value"] = {"$ref": "cycle"}
    decoder = EvidenceDecoder()
    with pytest.raises((ValueError, KeyError)):
        decoder.decode(view)
    assert decoder.shared == {}


def test_messages_and_review_recover_exact_visible_evidence_without_extra_calls(monkeypatch):
    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "1")
    session = AgentRunSession.create(ledger=None, system_prompt="system", question="q")
    for i in range(2):
        call = ToolCall(str(i), "look", "{}")
        session.append_assistant(AssistantTurn("p", "m", "tool_calls", "", (call,), reasoning_content="private marker"))
        result = ToolResult(str(i), "look", True, reading(i), observation={"id": i})
        message = session.tool_message(result)
        assert result.value == reading(i) and result.observation == {"id": i}
        session.append_tool_message(message)
    assert len(session.messages) == 6
    assert len(session.evidence_views) == 2
    assert session.request_messages("advice")[2]["reasoning_content"] == "private marker"
    recovered = review_evidence(session.messages)
    assert [r["result"] for r in recovered] == [reading(0), reading(1)]
    assert "private marker" not in canonical(recovered)
    messages, evidence = stage_review.request_messages("q", {"answer": "a"}, session.messages)
    review_value = json.loads(messages[1]["content"])
    assert EvidenceDecoder().decode(review_value)["evidence"] == evidence
    assert stage_review._pointer(evidence[0]["result"], "/data/rows/2/value") == 0


def test_native_fallback_and_terminal_messages_stay_in_original_format(monkeypatch):
    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "1")
    session = AgentRunSession.create(ledger=None, system_prompt="s", question="q")
    for name, terminal in [("query", False), ("submit_response", True), ("wiki", False)]:
        r = ToolResult("c", name, True, reading(), terminal=terminal)
        assert session.tool_message(r) == r.as_message()


@pytest.mark.asyncio
async def test_context_guard_measures_view_without_repeating_handler(monkeypatch):
    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "1")
    calls = []

    async def handler(args):
        calls.append(args)
        return {"rows": [reading()["data"]] * 10}

    reg = ToolRegistry()
    reg.register(ToolSpec("look", "d", {"type": "object", "properties": {}}, handler, max_result_chars=20000))
    output = await reg.execute(reg.prepare(ToolCall("c", "look", "{}")))
    assert output.ok and len(canonical(output.value)) > 20000
    assert model_size(output.value) < 20000 and len(calls) == 1


def test_compilation_avoids_size_paging_but_retains_every_row(monkeypatch):
    data = {
        "facts": [{"object": {"id": str(i)}, "facts": reading()["data"]} for i in range(15)],
        "coverage": {"scan": "complete", "listing": "complete"},
        "not_read": [],
    }
    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "1")
    result = ObjectReader._fit_page(copy.deepcopy(data))
    assert len(canonical(data)) > MAX_TOOL_RESULT_CHARS
    assert result == data
    monkeypatch.delenv("NEKO_EVIDENCE_COMPACT")
    original = ObjectReader._fit_page(copy.deepcopy(data))
    assert len(original["facts"]) < len(data["facts"]) and original["coverage"]["listing"] == "partial"


@pytest.mark.asyncio
async def test_real_runtime_keeps_model_and_business_call_counts_with_compaction(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import AgentAuditRegistry, AgentRuntime, build_agent_model_settings

    class Logger:
        def info(self, *args):
            pass

        warning = error = info

    async def one_run(enabled, references=False):
        monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "1" if enabled else "0")
        calls = []
        requests = []
        reports = []

        async def handler(args):
            calls.append(args)
            return reading()["data"]

        class Model:
            async def complete(self, messages, tools, *, timeout):
                requests.append(copy.deepcopy(messages))
                call = (
                    ToolCall("read", "look", "{}")
                    if len(requests) == 1
                    else ToolCall(
                        "submit",
                        "submit_response",
                        json.dumps({"answer": [{"text": "The lookup completed."}], "detail": "The lookup completed."}),
                    )
                )
                return AssistantTurn("provider", "script", "tool_calls", "", (call,), reasoning_content="continuation")

        async def save(record):
            reports.append(record)
            return {"report_id": record["report_id"]}

        registry = AgentAuditRegistry()
        from plugin.plugins.neko_rimworld.agent.tools import ToolSetup

        specs = [ToolSpec("look", "lookup", {"type": "object", "properties": {}}, handler)]

        async def provide():
            return ToolSetup(tuple(specs), references_supported=references)

        runtime = AgentRuntime(
            observation_tools=specs,
            tools_provider=provide,
            audit_registry=registry,
            logger=Logger(),
            model_factory=lambda _: Model(),
            report_sink=save,
        )
        settings = build_agent_model_settings(base_url="http://127.0.0.1:1", model="scripted", run_budget_seconds=90)
        result = await runtime.run("Perform the lookup.", settings)
        assert result["status"] == "ok"
        assert len(calls) == 1 and len(requests) == 2
        assert requests[1][2]["reasoning_content"] == "continuation"
        payload = json.loads(next(m["content"] for m in requests[1] if m["role"] == "tool"))
        assert EvidenceDecoder().decode(payload) == {"ok": True, "tool": "look", "data": reading()["data"]}
        return result, requests, reports, payload

    baseline = await one_run(False)
    compact = await one_run(True)
    assert baseline[0]["answer"] == compact[0]["answer"]
    assert not is_compiled(baseline[3]) and is_compiled(compact[3])
    assert compact[2][0]["evidence_views"][0]["format"] == FORMAT
    native = await one_run(True, references=True)
    assert not is_compiled(native[3]) and native[2][0]["evidence_views"] == []


def test_source_bound_is_not_removed_when_model_view_is_small(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import evidence_compiler as codec

    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "1")
    monkeypatch.setattr(codec, "MAX_SOURCE_CHARS", 1024)
    value = reading()
    result = codec.EvidenceCompiler().compile(value)
    assert not result.encoded and result.content == canonical(value)


def test_bad_identity_anchor_cannot_silently_bind_another_object():
    identity = {"id": "A", "name": "Actor A", "description": "long identity " * 30}
    value = {f"role_{i}": copy.deepcopy(identity) for i in range(20)}
    view = json.loads(EvidenceCompiler().compile(value).content)

    def corrupt(x):
        if isinstance(x, dict):
            if "$ref" in x and "id" in x:
                x["id"] = "B"
                return True
            return any(corrupt(v) for v in x.values())
        if isinstance(x, list):
            return any(corrupt(v) for v in x)
        return False

    assert corrupt(view)
    with pytest.raises(ValueError, match="anchor"):
        EvidenceDecoder().decode(view)


def test_rejected_view_never_publishes_definitions_for_later_messages():
    compiler = EvidenceCompiler()
    with pytest.raises(ValueError, match="view_limit"):
        compiler.compile(reading(), max_view_chars=32)
    next_view = compiler.compile(reading())
    assert EvidenceDecoder().decode(json.loads(next_view.content)) == reading()


def test_nested_repeated_mod_fields_keep_valid_source_depth():
    subtree = {"description": "unknown mod fact " * 600}
    for _ in range(36):
        subtree = {"child": subtree}
    source = {"left": subtree, "right": copy.deepcopy(subtree)}
    view = EvidenceCompiler().compile(source)
    assert view.encoded
    assert EvidenceDecoder().decode(json.loads(view.content)) == source


def test_expensive_factoring_falls_back_without_registering_hidden_values(monkeypatch):
    from plugin.plugins.neko_rimworld.agent import evidence_compiler as codec

    monkeypatch.setattr(codec, "MAX_SOURCE_CHARS", 10000)
    subtree = {"description": "payload " * 100}
    for _ in range(36):
        subtree = {"child": subtree}
    source = {"left": subtree, "right": copy.deepcopy(subtree)}
    assert len(canonical(source)) < codec.MAX_SOURCE_CHARS
    compiler = EvidenceCompiler()
    view = compiler.compile(source)
    assert not view.encoded and json.loads(view.content) == source


@pytest.mark.asyncio
async def test_registry_checks_actual_session_view_and_keeps_internal_value(monkeypatch):
    monkeypatch.setenv("NEKO_EVIDENCE_COMPACT", "1")
    compiler = EvidenceCompiler()
    decoder = EvidenceDecoder()
    registry = ToolRegistry(evidence_compiler=compiler)

    async def handler(args):
        return reading()["data"]

    registry.register(ToolSpec("look", "d", {"type": "object", "properties": {}}, handler, max_result_chars=10000))
    for i in range(2):
        result = await registry.execute(registry.prepare(ToolCall(str(i), "look", "{}")))
        assert result.ok and result.model_content is not None
        assert len(result.model_content) <= 10000
        assert decoder.decode(json.loads(result.as_message()["content"])) == result.value
        assert result.value["data"] == reading()["data"]
