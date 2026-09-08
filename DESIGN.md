# N.E.K.O RimWorld Design Brief

## Identity Lock

- plugin_id: `neko_rimworld`
- folder: `plugin/plugins/neko_rimworld/`
- name: `N.E.K.O RimWorld`
- entry: `plugin.plugins.neko_rimworld:NekoRimworldPlugin`
- main class: `NekoRimworldPlugin`

## Purpose

Run an independent RimWorld domain agent inside the plugin. N.E.K.O's outer
Agent supplies one question and receives one evidence-aware result; it does not
perform the plugin's model loop, game inspection, or domain aggregation.

## Version 0.5.0 scope

- Preserve the authenticated read-only protocol-v2 game bridge.
- Add one automatically routed entry, `rimworld_consult(question)`.
- Own an OpenAI-compatible model configuration and client inside the plugin.
- Create a fresh `AgentRunSession` and messages list for every call.
- Run an open native tool-calling loop with no hard step limit.
- Offer Mod-described `get_game_info` and `query_game` for observation and `submit_response` for
  agent-authored result delivery as package-local private tools.
- Select target objects, then combine their information sections into one nested
  observation; pawn status, mood, health, equipment and profile share identity.
- Read nested fields through one resolver graph. Support bounded collections and
  explicit definition-based stat selection without prebuilding whole pawn JSON.
- Return tool errors to the model rather than terminating the loop.
- Keep an append-only, process-local audit ledger for model output, tool calls,
  tool results, and lifecycle transitions.
- Expose configuration, a manual consultation, and the latest audit through the
  Hosted TSX panel.
- Preserve the complete model answer while the private agent authors a separate
  natural-language callback text below the host's plugin-result token cap.

## Out of scope

- Wiki/general search or page retrieval.
- Long-term agent memory or cross-run message reuse.
- `@llm_tool` registration.
- N.E.K.O character-model/provider reuse.
- Proactive messages, speech, or TTS control.
- Gameplay mutation or autonomous management.
- Gameplay domains beyond current map/pawn facts (such as colony stockpiles,
  research, production, power or event history).
- Persistent full run history or chain-of-thought storage. Explicit saved report artifacts are supported.

## Entry surfaces

- Agent-visible: `rimworld_consult` and `rimworld_report`.
- Hosted-only: `save_bridge_settings`, `save_agent_settings`, and
  `probe_game_overview`, each marked `metadata.agent_auto=false`.
- Agent-private: `get_game_info`, `query_game` and `submit_response`, registered only in
  the package-local tool registry and never published through the SDK.
- Outer-Agent handoff: short answers may pass through unchanged; long answers
  use the private `submit_response(full_text, agent_text)` tool. Only the
  agent-authored natural-language `agent_text` becomes `summary`; `reply` is the
  metadata-loss fallback. Runtime code validates transport constraints but
  performs no domain selection, ranking, summarization, or conclusion logic.

## Agent state machine

```text
CREATED
  -> MODEL_REQUESTING
  -> MODEL_RESPONDED
       -> TOOL_DISPATCHING -> TOOL_RESULTS_READY -> MODEL_REQUESTING
                                           \-----> FINALIZING -> COMPLETED
       -> FINALIZING -> COMPLETED

Any running state -> DEGRADED -> FINALIZING -> COMPLETED
Any running state -> CANCELLED
```

This machine validates and audits runtime lifecycle only. It does not prescribe
which tool to call or how many reasoning cycles to use. The prompt provides a
soft cycle target and increasingly recommends finalization; only I/O, memory,
cancellation, and total wall-time safety boundaries are enforced in code.

## Session and concurrency model

- One entry invocation equals one independent run ID, messages list, tool
  history, and ledger.
- Completed messages are never fed into another run.
- N.E.K.O's host already schedules triggers as independent asyncio tasks.
- Model I/O is truly async; the synchronous bridge query remains inside
  `asyncio.to_thread`.
- Agent runs never acquire the bridge reconfiguration lock.
- The bridge worker, Hosted contexts, settings actions, and other agent runs
  remain schedulable while a model request is pending.
- Shutdown cancels active agent runs before stopping the bridge.

## Audit contract

Every externally observable change is recorded with a monotonically increasing
event sequence, wall and monotonic time, cycle, state before/after, and sanitized
data. Model responses retain content, finish reason, usage, request ID, and
native tool-call JSON. Tool records retain raw/parsed arguments, results, error
codes, and latency.

Provider hidden reasoning is represented only by field name, character count,
and SHA-256. Credentials and Authorization data are never retained. The most
recent 20 runs are kept in process memory and cleared on restart; this is
operational audit state, not agent memory.

## Failure semantics

- Invalid/unknown tool calls become recoverable tool results.
- Bridge offline, stale, busy, timeout, and protocol failures become tool
  observations.
- Empty model turns receive a recovery prompt.
- Model/transport failure produces a structured degraded result.
- Exceeding the soft cycle target never terminates a run.
- Cancellation transitions the run to `cancelled` and is propagated.
- The host timeout remains the final process-safety fuse.

## Security boundary

- Pairing token and model API key are write-only Hosted inputs and plain-text
  per-user runtime configuration values.
- Source manifests and examples contain empty credentials only.
- HTTP with an API key is permitted only for loopback endpoints; remote keyed
  endpoints require HTTPS.
- Game labels and model output are untrusted data.
- Full prompts, raw frames, credentials, and hidden reasoning never enter normal
  plugin logs.
- Public SDK imports remain limited to `plugin.sdk.plugin`; all agent internals
  are package-local.

## Write workspace

`plugin/plugins/neko_rimworld/`

## Data path boundaries

The Mod owns one executable field schema, object selectors, section selections
and argument limits. It exports the schemas used by get_game_info and query_game.
Each template request contains a target and sections. Section fields for a target
merge into one selection tree, including that object's identity; repeated targets
share one output root and bindings retain each originating request. Template
expansion reads no game objects. It produces one variable-bound document that
uses the same serial resolver path as precise GraphQL.

Pawn facts occupy one canonical nested graph: status, mood, health, equipment,
profile and requested stats. A section may select fields across this graph, but
does not own another copy of those facts. Mood thought groups and raw memories
remain different collections; conditions reference body parts; weapons, worn
apparel and carried inventory retain their containers. Skills, traits and derived
stats have separate semantics. Stable object IDs, definition names and explicitly
sample-scoped references are not interchangeable.

IGameData separates live Verse getters from the engine for deterministic tests.
Child records remain query-scoped and fields are resolved lazily. Collection
windows expose their coverage and a continuation offset; root colonist discovery
uses its load-bound cursor. Neither mode freezes live data across requests. The
agent chooses relevance and next reads; the runtime does no automatic risk
ranking, investigation routing or domain interpretation.

CatalogTools caches the Mod directory per connection and injects tools/context
at run start; these descriptions are stable API knowledge, not dialogue memory.
The loop does not select templates or inspect game fields. Observation retains
individual query envelopes, issues and expanded-query execution metadata.
Terminal batches are checked before dispatch; no submission based on unseen
query results is accepted. Outer delivery stays natural-language only.

See DATA_PATH.md for the authoritative current operation, paging and budget
contract. Older colony_snapshot is removed with no compatibility branch.

## Model transport and cancellation

Version 0.6 uses strict bounded SSE assembly, separately configurable first
useful response and inactivity limits, and absolute request/run timeouts. Tool
execution only receives completed model turns. The finalization reserve is
prompt guidance, never a claim of evidence sufficiency. Model failures and
cancellations retain phase diagnostics and any provider usage already received.
No hidden reasoning content is persisted or surfaced. Defaults/limits are in
agent/model_client.py; the entry has cleanup headroom above the internal run.

## Hosted navigation and data loading

0.6.1 retains one surface/context and separates four pages under ui/pages/.
ui/controller.ts owns ephemeral form drafts and operation state; components do
not own in-flight entry calls. Lightweight dashboard summaries feed Home and the
run list. Session details are fetched by immutable run ID, with paged audit events
and stale-response guards. UI detail actions remain agent_auto=false and are
mirrored in manifest entries. No platform navigation or model tools changed.


## Submission drafts and report artifacts (0.7.0)

Each AgentRunSession owns a SubmissionDraft. Field validation commits valid fields
independently and gives structured repair feedback; a rejected replacement never
overwrites the earlier valid body. Changed body content invalidates its handoff.
The loop preserves drafts on model failure, timeout and cancellation, with explicit
unfinished state. Domain meaning and correction remain the agent's responsibility.

ReportStore saves immutable terminal artifacts via the SDK data path, separately
from short-lived audit. Public list/read exposes report IDs; full text is shown in
Hosted UI and can be silently exported to a host run. Only explicit report_id on
consult attaches historical content plus an evidence index. Historical evidence
reads are ordinary mappings, never live Observation values. No implicit latest
report, automatic memory replay, or domain summarization is introduced.


## Native Thing queries and schema discovery (0.8.0)

Mod 0.6.0 adds ThingSchema/ThingData and the testable ThingPaging selector. Original
Thing/ThingDef members, component base projections, ThingOwner and SlotGroup stay
separate. Pawn equipment now references the shared Thing projection and active
hauling is distinct from inventory. Query-only DTOs wrap references and bounded
collections; unsupported components are metadata, never inferred values.

get_game_info and query_game still forward intact to one executable schema.
describe_game_schema uses the Mod's static describe_schema operation, caches
schema-id checked results per setup, and returns metadata without polluting game
evidence. Startup injects a type/root directory instead of the full SDL. Existing
report delivery and outer-agent entries are unchanged. See DATA_PATH.md for the
breaking member names, exact targets, component fragments and scope limitations.
