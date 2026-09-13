# N.E.K.O RimWorld

The current checkout contains the v1.6 execution/query revision.
Version metadata remains 1.0.0. This source change is not a stable release or
proof that all natural-language answers are correct.

The v1.6 input candidates were implemented and tested through the actual runtime,
then withdrawn from defaults because normal-budget confirmation did not establish
a correctness gain. Existing research wording and native read-message history remain
the default. Named prompt sections support reproducible bench experiments.

Research requests and tool waits now stop at the research deadline, leaving the
configured finalization reserve with only submit_response available. This reserves
time but cannot guarantee the provider finishes a valid answer. Stream packaging
and effective answer/tool bytes are bounded separately (8 MiB and 2 MiB).

With the current Mod, the specialist Agent uses `query`, `describe_query` and
`submit_response`. The Mod owns the field vocabulary, selection grammar and
read semantics; the plugin forwards that generated directory, including field
notes. See [DATA_PATH.md](DATA_PATH.md) for the current contract. No additional
business-specific tool or focus preset is added.

Submission errors now follow the connected Mod's capability. In text mode,
answer parts contain only `text`; `language` belongs at the submission's top
level. Repair feedback reports the text budget and actual input problem,
without recommending unavailable observation-reference parts. The short
handoff remains capped at 180 tokens; `detail` retains requested factual
coverage and the existing reconfirmation protection.

The system instruction asks state reports to preserve observations, scope and
unknowns without adding unsupported background rules. This is an instruction,
not a semantic guarantee. Model results and remaining failures are recorded in
the separate rimworld-bench repository. The historical sections below describe
older compatible versions; current public-query behavior takes precedence.

Sources require their normal reload/rebuild to activate; editing this checkout
does not update an already loaded game assembly.

Output budget defaults to 32768 tokens and is editable under Settings → running
budgets (64–131072, subject to provider limits). Reasoning may consume this budget.
Length termination is reported as output-budget exhaustion with finish_reason,
requested limit and actual completion/reasoning usage. Existing explicit runtime
overrides remain in effect until saved through Settings. Time limits are separate.

Independent, memoryless RimWorld specialist agent backed by a read-only local
game bridge.

## Historical version 0.5.0 scope

- Authenticated IPv4 loopback bridge, default `127.0.0.1:48919`.
- One N.E.K.O Agent-visible entry: `rimworld_consult`.
- Plugin-owned OpenAI-compatible Chat Completions client.
- A request-scoped, open tool-calling loop with prompt-guided cycle targets.
- Three private tools: `get_game_info` for object and section combinations, `query_game`
  for precise GraphQL selection, and `submit_response` for agent-authored delivery.
- Nested pawn observations: current status, mood factors and memories, health and
  body parts, weapons/apparel/inventory, background, traits and skills. Derived
  stats can be requested by definition through precise queries.
- Independent messages and audit ledger for every consultation.
- Process-local bounded recent-run history; it is never reused as model memory.
- Hosted TSX setup, manual agent test, and model/tool/state-transition audit.
- Lossless full answers plus agent-authored, callback-safe natural language for
  the outer N.E.K.O Agent.

There is no Wiki or general search, persistent agent memory, `@llm_tool`,
proactive speech, gameplay mutation, or autonomous game actions in this version.

## Pair the game bridge

1. In RimWorld's N.E.K.O Mod Settings, generate a pairing token and copy it.
2. Start this plugin and open the **RimWorld Bridge** Hosted panel.
3. Under **Bridge setup**, paste the token and enter the same port used by the
   Mod, then choose **Save & restart bridge**.
4. Start or load a game and verify that the bridge is ready and the session is
   not stale.

The pairing-token field is write-only. Leaving it empty after initial setup
keeps the stored value.

## Configure the independent model

Under **Independent research agent**, configure:

- an OpenAI-compatible base URL, normally ending in `/v1`;
- the exact model name served by that endpoint;
- an API key, if the endpoint requires one.

The model adapter requests SSE streaming Chat Completions and assembles native tool calls before execution.
The API-key field is write-only and leaving it empty preserves an existing key.
An API key cannot be sent to a non-loopback plain-HTTP endpoint.

The model is owned by this plugin. It does not reuse the N.E.K.O character
model, prompt, conversation history, or memory.

## Consultation behavior

The outer N.E.K.O Agent calls:

```text
rimworld_consult(question)
```

Each call creates a new run with a new message list. The model may answer
directly, select an object and information sections through `get_game_info`,
request precise fields through `query_game`, or finish a detailed response through the
private `submit_response` tool. The prompt recommends a small number of cycles
but the runtime does not impose a hard step count. Tool and submission errors
are returned to the model so it can recover and continue.

Network requests, cancellation, response sizes, and total wall time remain
bounded for process safety. If the model cannot finish normally, the entry
returns a structured `degraded` result instead of stopping the plugin.

## Audit model

The Hosted panel shows the latest run's observable trace:

- model request and normalized response;
- assistant content and native tool calls;
- raw and parsed tool arguments;
- tool results and failures;
- every runtime state transition;
- model/tool call counts and elapsed time.

Hidden provider reasoning is not stored. If a provider emits a reasoning field,
only its presence, size, and hash are recorded. API keys, pairing tokens, and
Authorization headers are always redacted. Audit data is process-local and is
cleared when the plugin process restarts.

## Agent visibility

`rimworld_consult` and `rimworld_report` are exposed to automatic N.E.K.O Agent
routing. Hosted configuration and diagnostic entries use
`metadata={"agent_auto": False}` and remain callable from the authorized panel.
The private agent tools are ordinary Python callables and are not registered as
plugin entries or LLM tools.

The consultation result keeps the complete `answer`, per-query observations,
freshness, and audit data for the panel. Short answers can pass through
unchanged. For longer reports the private agent calls
`submit_response(full_text, agent_text)` and authors the concise natural-language
`agent_text` itself. Plugin code validates only transport constraints and never
selects facts, derives conclusions, or exposes private field formats to the
outer Agent. The entry returns `agent_text` as both `summary` and `reply`, and mirrors
`llm_result_fields=["summary"]` into entry metadata, so isolated-host metadata
reconstruction cannot silently degrade the callback to the run-status message.

## Development checks

This is a plugin repository, not a standalone N.E.K.O installation. Check it
out at `plugin/plugins/neko_rimworld` inside a configured N.E.K.O checkout; its
SDK, build commands and Hosted UI tests use the host project's dependencies.

From the N.E.K.O repository root:

```powershell
uv run --with pip neko-plugin sync neko_rimworld --clean
uv run pytest plugin/plugins/neko_rimworld/tests -q
$rimworldSources = @(rg --files plugin/plugins/neko_rimworld -g '*.py')
uv run python scripts/check_async_blocking.py @rimworldSources
uv run neko-plugin check neko_rimworld --release
cd frontend/plugin-manager
npm run check-hosted-tsx -- plugin/plugins/neko_rimworld
```

## Architecture

```text
NekoRimworldPlugin       public SDK lifecycle/UI/entry facade
BridgeServer             authenticated read-only game transport
observation.py           detached request/response observations, without field projection
agent/model_client.py    plugin-owned OpenAI-compatible client
agent/tools.py           private tool schemas, validation, dispatch
agent/loop.py            open request-scoped tool-calling loop
agent/audit.py           append-only state transitions and run registry
agent/handoff.py         composed answer and submission validation
agent/presentation.py    faithful Mod-described fact presentation
ui/panel.tsx             setup, result delivery, and audit inspection
```

The executable wire contract remains shared with the C# Mod's `Protocol.cs`.

## Object sections and exact queries

Upgrade both Mod and plugin to 0.5.0. Ship the complete built Assemblies folder,
including GraphQL.NET, GraphQLParser, Newtonsoft.Json and transitive DLLs.

`get_game_info(requests)` selects objects first, then their information sections:

```json
{
  "requests": [
    {
      "target": {"type": "pawn", "id": "pawn1"},
      "sections": ["status", "mood", "health", "equipment", "profile"]
    }
  ]
}
```

Use a `colonists` target with `sections: ["summary"]` and optional name/map/ID
filters to locate people; use a `colony` target with `sections: ["overview"]`
for loaded-map and game information. Names can match multiple people. Detailed
queries use the returned pawn IDs and currently cover spawned free colonists on
loaded maps, not every world pawn.

Multiple sections merge into one nested pawn object, including identity fields.
Repeated requests for the same target merge field selections. A batch can include
different targets, with separate sections for each. The Mod owns this expansion;
Python forwards the request unchanged and preserves its complete response.

`query_game(document, variables)` handles precise field combinations and further
pages of nested collections. Both tools execute through the same schema and field
resolvers in one game-thread sample. Fields are read only when selected. Old
flat colonist fields and `type/args` template requests are removed.

The five pawn sections are useful observation bundles, not separate reports:
status includes activity and important needs; mood includes game-computed thought
groups; health includes conditions and capacities; equipment keeps weapons,
apparel and inventory distinct; profile includes traits and skills. Unavailable
facts remain null or explicitly failed. Collection coverage distinguishes an empty
list from an incomplete or unread page. Memory references are local to a sample,
and separate requests do not imply a frozen state.

Mod exports the template descriptions, parameter schemas and executable SDL.
The plugin loads and caches these per connection; agent runs do not need to
re-discover the schema through another model tool call. No game tool is offered
when the authenticated schema cannot be obtained.

See [DATA_PATH.md](DATA_PATH.md) for examples, limits, paging/error semantics,
extension boundaries and verification.

Local development manifests declare all four public entries in top-level `[[entries]]`
tables so import-free host discovery can validate Hosted UI actions. Keep these
schemas, timeouts and agent visibility aligned with decorators; the plugin test
suite verifies their equality. Private model tools are not declared as entries.

## Request and usage display (0.5.1)

The panel shows model request attempts, returned responses, request failures,
private tool calls and API-reported input/output/total tokens for the latest run.
Cached-input and reasoning token details are subsets, never added twice.
Unknown usage after timeouts, cancellations or omitted provider usage stays
unknown (`—`); partially reported totals use `+ ?`. No price/billing estimate.

History totals cover only retained runs (latest 20 completed plus active), not
account lifetime. Reload clears them. Both successful and failed UI calls refresh
metrics; terminal duration remains fixed. Output/handoff text token counters
are separate from model API consumption.

## Streaming and deadlines (0.6.0)

Model requests use stream=true; stream_include_usage defaults true and can be
explicitly disabled for providers that reject that option. There is no silent
non-streaming retry or provider/model switch. SSE text and interleaved tool-call
fragments are assembled until finish_reason and [DONE]; partial streams never
execute partial tools. Usage-only tail frames are counted, including known usage
on a subsequently failed stream. Hidden reasoning is hashed/count-only.

Defaults: connect 10s, first useful model response 45s, stream inactivity 20s,
absolute model request 60s, absolute agent run 90s, finalization reserve
15s (at most half a short run budget). Production research calls and tool waits
are bounded by the research deadline; after it only submission remains available. Entry timeout is 100s and
Hosted UI waits 115s. Static and decorated entry metadata are kept aligned.

Per-request audit diagnostics include phase, exception class, response headers,
first useful delta and total time. The panel exposes the main timing fields.
Platform fixes unify Hosted UI/run metadata resolution, add IPC response
headroom and bound cancellation cleanup. Reload the plugin for its client code;
restart NEKO to load changed platform modules. RimWorld does not need restarting
for these Python-only transport changes.

## Four-page Hosted UI (0.6.1)

The single Hosted surface now has top navigation: Home, Sessions & Events,
Debug and Settings. Navigation is controlled inside the plugin; arrow keys,
Home and End work without an external router. At narrow widths the UI kit grid
collapses to one column.

- Home: current availability, lightweight usage/status and recent result links.
- Sessions & Events: select a retained run, then inspect result, paged events,
  observations or model requests; a separate view shows game/connection events.
- Debug: start one real consultation or a game-only probe, then jump to its run.
- Settings: bridge credentials, model configuration and editable execution budgets.

Drafts and in-flight tests live in the shared controller, so switching pages
neither discards them nor resubmits/cancels work. Context refresh does not overwrite
modified settings. Secrets stay in ordinary component memory, never local-state
persistence, and clear after successful saving. Old run selection stays pinned
unless Follow latest is enabled. Late detail replies cannot replace newer selections.

Dashboard context returns only run summaries. get_agent_run_details is a Hosted-only
entry with event_offset/event_limit (default50,max100); removed/reloaded history
returns run_not_retained instead of another run. Audit details retain the existing
in-memory history semantics; saved reports are independent artifacts. Model and budget
settings save together after backend validation; omitted budget fields retain values.

Checks from the workspace root:

```powershell
node --test N.E.K.O/plugin/plugins/neko_rimworld/tests/ui.test.mjs
```

The UI regression test uses the actual Hosted linker/compiler and UI runtime in
happy-dom; it does not call a paid model or change live credentials/settings.


## Partial submission and saved reports (0.7.0)

`submit_response` saves valid fields independently. If `agent_text` exceeds 180
 tokens, the tool error identifies `saved_fields`, `required_fields` and per-field
errors. The next call may contain only `agent_text`; the saved full report is
reused unchanged. Replacing the full report requires a matching handoff again.
A short plain-text correction also reuses a previously accepted full report.
Malformed JSON cannot be salvaged as a new field, but does not erase prior valid
fields or the current run's observations.

At run termination, a validated full text is persisted as a `complete` report or
an explicitly unfinished `draft` (including model failure, deadline and handled
cancellation). This does not recover a process crash before persistence. Saving
failure is explicit in `result.report.saved`; it does not discard the audit result.

- Open **会话与事件 → 完整报告** to read the full Markdown, copy the body/ID,
  or select **基于这份报告追问**. The latter attaches that exact historical report
  to a fresh consultation. **取消报告引用** returns to a fresh question.
- `rimworld_report(action="list", offset=0, limit=5)` lists retained metadata.
  Its short outer projection supplies exact IDs and a continuation offset.
- `rimworld_report(action="read", report_id="<32 hex characters>")` returns
  the full report and attempts a silent full-text artifact export to the host run.
  The UI remains available when host export is unavailable.
- `rimworld_consult(question="Explain this report", report_id="...")` explicitly
  attaches the full historical report and an evidence index. The private agent
  may use `read_report_evidence(index)` for original observations. Old evidence
  is never recorded as a fresh game query. Omitting report_id attaches no history.

The ordinary callback remains agent-authored natural language within 180 tokens.
Report IDs and bodies are available through the declared retrieval API and panel;
code does not append private metadata to the original agent-authored callback.
Exports use silent delivery, not automatic dialogue injection or speech.

Artifacts live under the SDK's per-plugin data directory, in `reports/`, using
atomic JSON writes and exact immutable report IDs (the internal run ID). The
latest 100 reports survive plugin reload; older reports are evicted. A report is
limited to 1 MiB: if observations exceed that budget, full text and sample IDs
remain, and `evidence_status=omitted_size_limit` explicitly reports omitted evidence.
The full-text transport limit remains 12,000 characters. Reports contain answers,
questions and observation provenance, not hidden model reasoning or entire run
message histories. This is explicit artifact reuse, not automatic agent memory.
Reports created before 0.7.0 are not automatically migrated from ephemeral audit.


## Native map things and schema lookup (0.8.0)

Requires Mod 0.6.0. Restart RimWorld to load the rebuilt DLL, then reload the plugin.
The native object path adds map things, ThingDef lookup, storage groups and the
native resource counter. Existing pawn equipment now preserves the actual
weapon/apparel/inventory/carry-tracker relations and shares Thing identity with
map queries. get_game_info still accepts target + sections; query_game selects
exact members. describe_game_schema supplies supported type/member details on
demand without creating game observations. See DATA_PATH.md for examples and
paging. This version removes flattened Item fields; no old-schema compatibility
branch is provided. No inventory management or hauling actions were added.

## Bridge restart reliability (0.8.1)

On POSIX systems the loopback listener can immediately reclaim its configured
port after closing an authenticated Mod connection. Saving unchanged bridge
settings keeps the active listener instead of disconnecting the game, while a
real token or port change still performs the transactional stop, start and
configuration update. A failed save refreshes the Hosted panel so connection
badges reflect the backend state without discarding the user's draft.

## Provider stream diagnostics (0.8.2)

Structured provider error events now retain bounded, sanitized `code`, `type`,
`param` and `message` fields in the run audit, together with payload size and a
SHA-256 fingerprint. The same safe fields are written to the plugin log with
the run ID, model request ID and request phase. Raw error bodies, request
content, Authorization values and API keys are not logged.

## Safe model request retries (0.8.3)

Each Agent cycle may make at most three provider attempts. Retries use short
backoff and are limited to transient transport failures, first-response
timeouts, retryable HTTP statuses, incomplete empty streams, and provider error
events that identify overload, rate limiting, timeout or temporary upstream
unavailability. A request is never retried after any useful text or tool-call
delta. Retry attempts reuse the same messages and tool schemas, remain inside
the run budget, preserve finalization time, and are audited and metered
separately. Permanent request/authentication/format failures degrade immediately.

## Actionable failure summaries (0.8.4)

The Home and run-result views show a direct diagnosis without requiring users
to inspect raw audit events. The summary identifies provider overload versus a
final timeout or other request failure, reports attempts and retries, preserves
the latest sanitized provider message, states whether game observations were
already collected, and recommends the next action. Only unresolved failures
after the most recent successful model response contribute to this diagnosis.

## Lightweight live progress (0.8.5)

Run summaries expose a monotonic revision, terminal flag, current stage and a
payload-free latest-event projection. While a run is active the Hosted panel
polls only this lightweight context with a single in-flight refresh, displays
progress at a one-second foreground cadence, and does not repeatedly download
the full audit. Full details load once when the run becomes terminal or when the
user explicitly requests them. Detail reads are single-flight, coalesce one
pending refresh, and retain the last successful content after a timeout.

## License

[MIT](LICENSE). Third-party dependencies retain their respective licenses.
