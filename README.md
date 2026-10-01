# N.E.K.O RimWorld

### Interaction contracts

The 180-token conclusion limit remains mandatory. Text-only answer edits retain
saved detail; changing explicit observation references still requires matching
detail. Definition selectors accept boolean held/fog flags as inapplicable and
remove them before native reads. A physical ID already observed in this run can
be recovered from `name`, preserving map scope and refusing name collisions.
Unresolved references distinguish missing objects from ambiguity. Health claim
checks only hard-reject a mechanically bound subject, including a named table
row; unbound prose remains subject to content review, not a forced extra read.

### Def-name recovery (`NEKO_DEF_NAME_RECOVERY=1`, opt-in)

Definition selectors keep the native exact lookup as the only arbiter of
existence and quantity. When the local Def knowledge index is attached and
this flag is on, a requested name that is not exact-known is normalized by
word order/case (the mechanism ported from the 2026-10-01 feasibility audit
controls: canonical round-trips, unique word-order hits, collision and
incomplete-catalog refusal). A unique canonical hit is resolved within the
same production call and disclosed on the result as `name_resolution`;
collisions and incomplete catalogs refuse auto-selection. A name still
missing after the native scan receives names-only `discovery` candidates
(token hits plus bounded label containment, which covers localized colloquial
names); candidates are never computed values and never replace the requested
term. A stale index degrades to an ordinary honest miss. Flag unset keeps
every lookup byte-identical; `agent/def_names.py` owns the mechanism.

### Current model context: ordinary JSON only

There is one input-reduction candidate: `NEKO_CONTEXT_PRUNING=1`. It is off by
default and remains experimental: the historical 30-task review found two major
content regressions (components omitted and toxic gas missed on an approach path).
It adds no model/tool calls. Native observations and audit values remain intact;
the author-facing view selects explicitly requested content in the same call.

The former `NEKO_EVIDENCE_COMPACT` modes are retired from the runtime. A nonempty
old setting fails explicitly; it is not silently mapped to a different algorithm.
Historical table/reference and aggressive codecs now live in Bench's
`experiments/legacy_input_encoding`, only for explicit offline replay. Reproduce
old live studies with their sealed plugin/executor copies.

`agent/model_context.py` owns the switch and size boundary;
`agent/context_pruning.py` owns JSON content projection;
`agent/context_tools.py` owns matching tool descriptions. Review receives the
author's ordinary JSON directly, without a second codec or display projection.
JSON Schema factoring remains a single shared helper, not an alternative
evidence representation. The existing cumulative public-input limit remains
`NEKO_PUBLIC_INPUT_TOKEN_LIMIT`; it is an accounting guard, not provider billing.

Current quality research is in Bench's `evaluation` package and
`planning/EVALUATION-PROTOCOL-v1.md`. A source checkpoint is not a release,
deployment or quality certification. See Bench's
`planning/WORKSPACE-CONVERGENCE-20260930.md` for the current development boundary.

The current checkout retains the v1.6 Agent runtime and supports the v1.6.1
query revision supplied by the Mod.
The local Def Wiki is the v2.0 feature milestone described below; package
metadata remains 1.0.0 under the repository's existing release convention.
This milestone is not a claim that all natural-language answers are correct.

The v1.6 input candidates were implemented and tested through the actual runtime,
then withdrawn from defaults because normal-budget confirmation did not establish
a correctness gain. Existing research wording and native read-message history remain
the default. Named prompt sections support reproducible bench experiments.

Research requests and tool waits now stop at the research deadline, leaving the
configured finalization reserve with only submit_response available. This reserves
time but cannot guarantee the provider finishes a valid answer. Stream packaging
and effective answer/tool bytes are bounded separately (8 MiB and 2 MiB).

With a compatible current Mod, the specialist Agent defaults to `look`, `count`
and `submit_response`, plus `wiki` when a local Def directory is ready.
`query` / `describe_query` remain the native fallback when the semantic adapter
is disabled or unavailable. The Mod owns the underlying field vocabulary,
selection grammar and read semantics, including field notes and native enum
value domains. Invalid enum names are rejected by the Mod before reads; the
plugin does not maintain a second value table. See [DATA_PATH.md](DATA_PATH.md)
for the native contract and the semantic-surface section below for Agent usage.

When the connected Mod advertises `spatial_query.v1`, the semantic surface also
provides `inspect_space(from, to)`. It is a single current-tick native reachability
read from one Pawn to one Thing or map cell; names and IDs are accepted directly,
so a preceding `look` is not required just to resolve objects. Ambiguous names
return candidates. The result does not claim WorkGiver eligibility, reservation,
route distance, Room membership or future success.

Submission errors now follow the connected Mod's capability. In text mode,
answer parts contain only `text`; `language` belongs at the submission's top
level. Repair feedback reports the text budget and actual input problem,
without recommending unavailable observation-reference parts. The short
handoff remains capped at 180 tokens; `detail` retains requested factual
coverage and the existing reconfirmation protection.

### Local Def knowledge (optional offline tool)

When the connected game Mod has published a valid `ready.json` directory, the
specialist agent advertises one additional tool: `wiki(query)`. It reads the
local `neko.defs.v1` payload written automatically by NekoRimWorld; it does not
run an external command, use HTTP, or require the game bridge to be connected.
The older `current.json` file remains a compatibility input for explicit legacy
or offline import, not the production fast-path publication contract. The
location is `NEKO_RIMWORLD_KNOWLEDGE_DIR` when set, otherwise the platform
LocalApplicationData directory under `neko-rimworld/knowledge`.

The game publisher now creates `ready.json` (`neko.defs.index.v1`): a compact
names/descriptions directory with byte offsets and per-entry SHA-256, pointing to
an immutable `snapshot-<id>.json` containing the complete definitions. The plugin
loads and validates this small directory at startup; it does not reparse/copy the
whole payload or build SQLite before use. Each lookup seeks to the selected record,
checks its SHA-256 and validates that record before returning it. The payload's
whole-file hash is publisher provenance; full-file verification is an explicit
offline audit, not secretly included in the sub-second startup claim.

A single background worker checks for a replaced directory every five seconds.
Consultations pin an immutable file version; failed replacements keep the previous
version with unverified applicability. Production never silently falls back to a
slow full-file import when `ready.json` is missing. The old `current.json` importer
(ijson + SQLite in `knowledge-index`) remains available for explicit legacy/offline
use, outside the one-second fast-path contract. No SQLite library is shipped into
the game. Old generations remain available for active runs; no lease/refcount system
is used. `compose_wiki_tools` is shared by product and bench.

The tool accepts one query string of at most 4096 characters: a name, collected
label, full `type/defName` key, or description keywords. This is keyword lookup,
not natural-language question answering. It also searches labels and descriptions
already collected in facts, including Trait `degreeDatas[].label` and Thought
`stages[].label` when the root label is empty. Discovery returns compact candidate
cards. A full key fixes the object and never falls back to another definition.
Append literal fields/groups, e.g. `Verse.RecipeDef/Make_Wort products[0].count
recipeUsers`, to read them with indexed siblings. Small entries fit in full;
large entries return whole groups plus `not_read` and a structural `directory`.
Facts, types and paths are never clipped. Fields that do not exist appear in
`missing_fields`; explicit null remains a fact. Referenced Defs may receive their
original label through a bounded one-level read. Relations remain directed, with
no inferred exclusivity. No field-intent alias table is used. Full source fields
remain in the pinned snapshot regardless of response selection.

Snapshots remain `neko.defs.v1`, with string `name`/`value` facts and a 512 MiB
input limit. Optional fact `type` is validated as a string and retained within
response bounds: CLR names distinguish scalar types, and `type="null"`
distinguishes actual null from the string `"null"` with `type="System.String"`.
Older facts without `type` remain supported without inventing a type.
`status` describes object resolution/source integrity; `response_truncated`,
`read_complete`, `not_read` and `directory_truncated` describe the returned view.
Explicit leaf fields are budgeted before optional siblings. `read_complete` refers
to the requested paths; `context_not_read` / `context_not_read_count` separately
record omitted sibling context. A successful leaf read does not expand a large
parent directory just because optional context was omitted. Smaller requested
groups are budgeted before larger groups, which may still require a follow-up.
Older snapshots without diagnostics, warnings or provenance metadata
remain supported. Entry diagnostics (`path`, `kind`, `reason`) are validated and
returned within bounds, prioritizing `incomplete`; omitted diagnostic detail is
marked. Incomplete diagnostics and actual snapshot issues yield partial results.
Provenance warnings and `provenance_complete=false` alone do not lower definition
completeness. Responses stay within 8 KiB. Keys are returned exactly, never clipped
into a different query identity; a key that exceeds 4096 characters or cannot fit
the response budget is returned as null with an explicit `key_unavailable` reason.

The tool returns an ordinary tool mapping, not an Observation: the snapshot describes
loaded definitions and relations, not the current save, inventory, ownership,
research completion, or final computed effect. Its snapshot hash, game/language
metadata and unverified applicability are retained separately in reports. A
missing or incomplete snapshot is never advertised as a successful empty wiki.

Use `look`/`count` for current-save facts. The model is not forced to call
`wiki`; purely state questions should not pay for definition lookup. The tool
is removed during finalization so the reserved submission phase cannot start a
new knowledge read.

### Reference delivery (opt-in, not promoted)

`NEKO_PUBLIC_REFERENCES=1` enables reference parts only when the connected Mod
advertises `observed_values.v1`. The public-query default remains text-only.

### Semantic surface (shipped default)

The agent reads through two closed-enum tools instead of the Mod's raw query:
`look(object, aspects, detail?)` for facts and `count(object, metric, by?,
detail?)` for measurements. The model never writes field paths, native argument
keys or block structure, and `detail` defaults to 0 so a measurement returns
counts rather than unrequested instance rows.

The reason is token cost, not taste. The query interface needs the Mod's field
directory in the prompt on every request so the model can name fields; that
directory is 48,927 characters, about 84% of a first request. Replacing the
vocabulary removes the need for it. In the paired study the same questions held
quality level (12/12 both arms) while using two thirds fewer tokens, with the
same request count and the same number of model attempts.

`NEKO_SEMANTIC_SURFACE=0` returns to the Mod's own query interface without a code
change. The surface also steps aside on its own, and the consultation then runs
on the Mod interface, when:

- the connected Mod advertises no query directory (legacy catalog), or
- no `query` tool is present to read through, or
- the directory does not compile, or
- the caller attached a historical report. That path registers
  `read_report_evidence` and appends reference text to the prompt, and an
  interaction adapter rebuilds both every cycle, so the two cannot be combined.

The closed enum covers the measured demand shapes (one object's condition, lookup
by identity, scoped populations and measurements). A request outside it is not
expressible; that is the accepted cost of the interface, and it is why the study
that promoted it is paired rather than single-arm. Results are rendered as plain
text with their scope, coverage, not_read and native semantics; unknown is never
lowered to zero.

Reference answer parts stay off while this surface is active, even when
`NEKO_PUBLIC_REFERENCES=1` and the Mod advertises `observed_values.v1`. A
reference part cites a data-relative path into the native payload, and this
interface never shows the model a path: results arrive rendered. The capability
would be unusable rather than merely untested, so it is suppressed and the run's
`interaction_state` records `references_suppressed: true`.

Reference submissions preserve the program-rendered `fact_tokens` through the
runtime's final result, including plain-text detail repair. Budgets are separate
but **not additive host allowances**:

- Model-written conclusion: at most **180 tokens**.
- Entire rendered handoff (facts + conclusion): at most **200 tokens**, matching
  the unchanged host result parser. Oversize submissions return the recoverable
  `handoff_transport_over_budget` error, never a silently truncated success.
- At most **16 paths per part and per observation**, including nonadjacent parts;
  at most **32 distinct observation/path pairs** per answer, across at most
  four observations. Relative and leading-slash paths count identically.
- The 1200-token fact ceiling is a renderer safety guard, **not extra callback
  space**. `answer + detail` still fits 12000 characters. Preserve required
  coverage in the full report when narrowing the short conclusion.

A final transport validation failure is `degraded`, not `ok`. The original
report remains available; the callback receives an operational failure notice.
No host code or global callback limits are changed. This fixes transport
correctness, not the information capacity of the host: a long reference report
still cannot be injected in full through a 200-token summary.

Offline regressions in `tests/test_reference_delivery.py` exercise the actual
AgentRuntime, host result parser, event emitter and text/voice callback renderers,
with scripted model/game responses and a captured network-send boundary. They
cover unknown/lower-bound preservation and oversize repair, not a live host
process, Unity timing, callback queue saturation, or final persona-model quality.

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
agent/semantic_tools.py  shipped interface: closed-enum look/count, or the Mod's query
agent/semantic_surface.py closed-enum vocabulary compiled onto the Mod query
agent/query_surface.py   field types and argument schemas from the Mod directory
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

## V2.1 identity and scope repair

Spatial name/ID lookup now runs within one request over cooperative main-thread
slices (2 ms yield target, 12 ms slice guard, 2-second total request lifetime).
Individual native getters and membership capture cannot be preempted; these are
not hard frame-time guarantees. Each reference captures up to 100000 candidate
identities at its lookup start; held membership remains bounded and cycle-aware.
No persistent index or background Verse access is added. Lookup start/end ticks
and membership semantics are returned; reachability is checked on the resolved
objects at the final tick. Objects created after membership capture are outside
that lookup. The map is bound once at request start. Unknown remains unknown.

The plugin accepts `map` for Pawn reads and supports `id` as the singular alias
of `ids`; the map constraint is carried through lookup and final read. The look
schema advertises per-kind aspect combinations. Failed definition/name resolution
reports lookup source/scope/coverage separately: the requested instance read or
count is **not_run**, never a complete zero count. A complete missing exact-ID
public query is an empty result; an interrupted lookup remains partial.

## V2.1-B optional native path summary

`inspect_space(from,to,detail="route")` uses the existing spatial request to check
reachability and, if true, run at most one native `FindPathNow`. Omitting `detail`
or using `summary` retains reachability-only behavior. Discovery advertises
`spatial_query.route_summary = native_path_summary.v1`; older Mods do not expose
the route parameter. No preliminary `look` or separate reach request is required.

A found `route` reports `steps` (grid transitions), `grid_distance` (sum of cardinal
1 and diagonal sqrt(2) cell widths), `native_cost` (native weighted search cost),
and start/end map cells. The native GenPath endpoint normalization is applied
before pathfinding: a Thing's returned endpoint need not be its occupied cell.
The path is not guaranteed shortest; cost is not seconds, elapsed ticks or ETA.
Paths are never issued to a Pawn and are disposed after reduction; no full node
array or persistent path handle reaches the Agent.

The route status is `found`, `not_computed` (lookup/unsupported/unreachable), or
`unavailable` (pathfinding/projection failure). A failed route leaves a completed
reachability result usable. The synchronous game call cannot be preempted; a
completed valid summary that exceeds the cooperative 12ms target is preserved
with `route_budget_overrun` and `read_budget_exceeded`, not discarded or retried.
Node reduction remains bounded. This is not a hard frame-time or travel-time
guarantee, nor a diagnosis of which obstacle caused a negative reachability result.

## Interaction contract refinement

`look` accepts `object={kind:"map",map:"Map_0"}` as an alias for the map ID.
Competing id/name/map selectors are rejected before any read.

Agent-facing route results call the length `path_length`: the sum along every
segment of the returned path, not straight-line endpoint distance. The native
wire retains `grid_distance` for older consumers; raw observations are unchanged
by the Agent projection, which exposes only one length field.

When observation references are disabled, `submit_response.answer` is now one
plain string. Ordered text parts and an unambiguous `{text:...}` object remain
accepted for compatibility, then pass through the same draft/evidence/token
validation. Unknown nested fields are not discarded. Reference-enabled tools
retain their parts schema. The 180-token answer, 200-token host transport and
12000-character report limits are unchanged. Guidance aims below the maximum
and puts every explicit question's conclusion/unknown in the short handoff,
with tables and supporting facts in detail. This is not an automatic semantic
coverage checker or a guarantee against model omissions.

For route-capable connections, `inspect_space.to` also accepts a bounded list of
1–8 targets. Comparisons can set `order_by` to `path_length`, `steps`, or
`native_cost`; this requests route detail and sorts valid returned values in
code. Equal values share rank, zero remains valid, and unknown or unsupported
paths remain unranked. Every row retains its original target index, identity,
and sample. Reads are sequential, not an atomic snapshot; a world change aborts
the call and different resolved actors prevent ranking. The single-target
contract is unchanged and no game action is executed.

The route projection explicitly returns `optimality.status="not_evaluated"`:
neither shortest nor non-shortest has been established. This prevents the
contract from conflating an absent shortest-path proof with a non-shortest path,
but does not guarantee that a model will paraphrase it correctly.

## Rescue-study contract repairs (2026-09-23)

- A later complete observation can supersede an earlier partial list only for the
  same connection/colony/load/tick, subject, relation path, resolved scope and
  total. Snapshot sequence must not decrease. Original evidence is retained;
  different ticks and unknown sampling identity remain conservative boundaries.
- `look`/`count` accept `kind:things,ids:[...]`; ID and name/definition/category
  selectors cannot compete. The native engine pushes exact top-level id.eq/in
  into candidate enumeration, preserving map/held/fog/definition scope. This
  avoids spending the 8192 candidate budget on unrelated objects; it is not an
  O(1) index and does not remove the native time budget. Older Mods may still
  hit their original broad-scan limit for this selector.
- `inspect_space.to.kind:pawn` is an accepted alias; the adapter uses the existing
  native Thing reference and rejects a resolved non-Pawn. No native wire enum
  change is required. Ambiguous names still require disambiguation.
- `things.category` advertises the connected game's native ThingCategory values;
  `thing_defs.category` retains its separate ThingCategoryDef content semantics.
- Identity reads include supported spawned positions. Spatial results provide
  the target's own `position` separately from `route.end`, the stopping cell.
  Unspawned objects have no own position; absent fields on older Mods remain
  absent, never inferred from Touch endpoints or holder positions.
- Health summaries carry adjacent field semantics: health_percent is an overall
  health fraction, bleed_rate is blood-loss fraction per game day (60000 ticks),
  pain is the game's pain fraction. No survival estimate is invented.
- Profile reads include the existing game-generated trait description/tooltip
  when supported. `degree` is a variant code, not a numeric stat bonus;
  suppression remains explicit and a trait bonus is not the final Pawn stat.

These are data/contract repairs, not a guarantee of correct model prose. No
region/safety/rescue-action tool or task-specific scoring rule was added.

## V2.2 world observation

When the connected Mod advertises `world_observation.v2.2`, the Agent also gets
`survey_space` and `inspect_route`. Older V2.2 contracts also expose `inspect_state`;
V2.3-capable connections consolidate that entry into `look` as described below. Use a region to discover objects/counts/environment, actor+target to
read a path and its environment in one call, or a target list to read applicable
state facets. State context distinguishes `user` (recipient) and `actor`
(traveller/interactor). Results retain original observations, sampling interval,
coverage and unsupported facets; no universal usable/safe verdict or game
mutation is provided. Cursor pages retain detached facts from the original
observation, not fresh state. Older Mods do not advertise these tools.


### V2.2.1 redo

The earlier summary-pruning/prompt candidates remain experiment artifacts.
Production tools keep their names, inputs, full Pawn evidence and existing
summary/details behavior. The Mod now attaches object-selection scope,
reservation direction and resolved context to observations, and formats roof
support identity/location references before paging. Inspect object state when
full containment/component metadata is needed; support references are not a
second full state listing.

Plain-text submission wording is maintained in agent/prompt.py and imported by
the submission schema and finish reminder. This is a maintenance refactor:
system/finalization prompts and the model-visible submission schema were checked
byte-for-byte against c59fa5e. The new wording trial was not promoted. Existing
180-token/12000-character limits, soft cycle guidance, name-direct resolution and
semantic validators are unchanged.

### V2.2.2 display ordering

The three world-observation tools present Pawn injuries/capacities before the
native overall-health summary, with state/stat/work facets nearer the front.
This changes dictionary order only: every fact, path, array order, unknown,
coverage flag and scope remains. The original native evidence is not mutated.
Legacy look/count, tool inputs, prompts, budgets and validators are unchanged.

Routing and completion-guidance candidates were evaluated separately and
reverted. Local injury-recognition results support the display adjustment, but
paired native regression does not demonstrate an overall accuracy or efficiency
improvement; complex rescue remains unreliable. See the bench V222-RESULTS report
for negative results and accounting limits. No new native Mod build is required.

### V2.3 consolidated object reader

On a Mod advertising the extended state contract, `look(object, aspects?, detail?,
stats?, actor?, user?, offset?)` owns object facts. `inspect_state` remains a native
compatibility operation, but is removed from the Agent tool list. Older connections
keep their previous tools. Physical objects and cells default to `state`; request
`identity`, `health`, `skills`, `gear` or `work` for smaller reads. Mood, biography,
resources and definitions retain their established readers. Explicit `stats` uses
native StatDefs; `actor` is the interactor/carrier and `user` the recipient, each
specified by one ID or exact name.

`look` and `count` accept `object.capabilities` for things, using the Mod's advertised
capabilities and intersecting other selectors. Discovering medicine or sleeping
places no longer requires an environment survey. Use `survey_space` for nearby or
regional environment, `inspect_route` for route environment, and `inspect_space`
for lightweight reachability and ranking. No universal usability/safety verdict is
provided.

Known single objects with native aspects resolve and read in one native operation.
Population selection batches state internally; every original sample and partial
result remains recorded. `page.next_offset` continues object rows using a fresh
read, and size bounds remove whole rows rather than silently pruning facts. Nested
list totals and completeness remain explicit. `detail` bounds Pawn/work lists;
other facets retain their own limits. Large single-object results can still require
fewer aspects. Existing held/fogged/map defaults and exact name ambiguity remain.

The sealed 12-episode regression supports simpler discovery and object access,
not a general reasoning-quality claim. Complex rescue still failed; see the bench
`planning/V23-RESULTS.md` for the full result and post-run deterministic repairs.

### V2.4 activity evaluation (opt-in)

The plugin contains an opt-in generic activity evaluator for bounded `move` and
`transfer` activities. It compiles actor/subject/destination relationships into
the existing read-only `look` and `inspect_route` operations, preserving each
stage's actor, origin, destination, observation and coverage. A transfer has an
approach stage (actor position to subject position) and a transfer stage
(subject position to destination using the same actor with a hypothetical start).

This is a mechanical evidence evaluator, not a rescue planner or action runner.
It does not choose a best actor, calculate a safety score, execute a Job, predict
carry speed, or claim that a native bed predicate proves post-carry eligibility.
Observed route metrics are ranked only within their declared stage and metric
scope; unknown, partial and uncomputed values do not become zero or safe.

Set `NEKO_ACTIVITY=1` to expose the single `evaluate_activity` tool alongside the
existing semantic tools. The default is unset and keeps the existing Agent
surface unchanged. The tool accepts one fixed activity description for `move` or
`transfer`; stages, route metrics and unknown handling are implemented internally
rather than selected through a second planning language. It has no new Mod wire
operation and requires a connected Mod advertising world observation for route
stages.

The evaluator reuses the existing object and route observations. It is still
mechanical evidence, not a rescue planner or action runner: it does not choose a
best actor, calculate a safety score, execute a Job, predict carry speed or claim
that a bed predicate proves post-carry eligibility.
# Context efficiency and optional stage review

### Content-pruning candidate (`NEKO_EVIDENCE_COMPACT=3`)

This opt-in replaces the v1/v2 evidence codecs with ordinary JSON. It removes
unrequested content and duplicate copies; it has no shared-value dictionary,
delta patches, extra planner or summary model. Modes 1/2 remain for existing
experiments. The unset/default path is unchanged pending quality acceptance.

In this mode, `look.aspects` is explicit and `state` means physical/device state.
Pawn health, skills, work and gear use their own aspects. `focus` requests named
skills/work types/capacities and their aspects in the same call; completeness is
reported for that selection, with unmatched names explicit. Full body-part lists
and implementation diagnostics are optional `include` values.

With `NEKO_ACTIVITY=1` and native world support, `evaluate_activity` is the single
public path/context matrix tool. `move` and `transfer` return paths;
`kind=state` checks destination state for each actor/user without calculating
paths. Names and destination selectors resolve in the call. Extra approach,
environment, destination state and actor facts are requested in that call rather
than always being read. `environment` is required when assessing path/corridor
hazards; omitted environment is never evidence of safety. Native readers remain
internal; the overlapping `inspect_space`/`inspect_route` public choices are
removed only when this batch interface is available. Legacy connections are not
advertised native capabilities they cannot execute.

Only pure text handoff-length repairs use a small editing context containing the
original question and saved answer. The existing detail stays saved; unsupported
claims and other errors retain the research context. Limits are unchanged.

`NEKO_PUBLIC_INPUT_TOKEN_LIMIT=<integer>` optionally limits the cumulative public
input estimate across all requests, retries and review in one consultation.
Admission refusal is a degraded result, not a successful answer. This counter
excludes provider framing/private continuation and is not a billing guarantee.
Original observations remain internal/audited; a pruned view is not a lossless
copy of them. See the Bench content-pruning report for exact tested source and
the separate input, delivery and factual-quality results.

Runtime timing advice is sent after the stable conversation prefix. It is not
inserted into the first system message on every request. Public observations and
provider-required private continuation remain intact; private reasoning is never
copied into review input or persisted as plaintext.

With a Mod advertising `spatial_object_state.v1`, `survey_space` and
`inspect_route` return spatial condition, passage and hazard facets for nearby
objects. They do not imply that Pawn health/work/gear, bed suitability,
reservations, storage or production were inspected. Use the existing `look`
object/state interface for those facts; environmental coverage remains explicit.

`[neko_rimworld.agent] review_before_delivery = true` enables one advisory check
after a valid explicit submission, within the **same** run budget. Default is
`false`: the efficiency implementation is verified, but complex-rescue quality
has not passed acceptance. No game-query tools are given to the reviewer.
No reported issue means the original answer is reused. Otherwise the original
author continues with its existing evidence and private continuation; it can
reject unsupported objections. There is no second review. Failures/skips are
reported, not treated as approval. A failed revision remains degraded and is
explicitly labelled if the pre-review draft is shown. The 180-token handoff
limit is unchanged.


### Contract and review validation (2026-09-25)

`look` publishes the same per-kind state vocabulary and context constraints as its
reader. Legacy quantity/distribution combinations remain compatible; prefer
`count` for aggregates. `count` publishes valid population/metric/grouping
combinations, including Pawn populations grouped only by map.

With the matching Mod, `detail` bounds long Pawn/work lists while skills,
capacities and work-type assignments retain all observed definitions. Existing
partial/unavailable flags remain authoritative. `inspect_space` starts at the
actor's current position; `inspect_route(actor, start, to)` permits a hypothetical
origin while retaining the actor's traversal rules. Neither predicts a carry Job
or an ETA.

Optional `review_before_delivery` still defaults to false. A single critic now
checks essential claims and cites visible evidence with read-only tool meanings.
Its coverage and supported verdicts are self-reported assessments, not approval.
Unresolvable citations are retained as explicitly unverified advice, never guessed
or silently accepted; they require author verification. Review errors retain the
existing degraded/unreviewed behavior. There is no second critic or automatic
format-repair call. The short-answer limit remains 180 tokens.

The corresponding fixed-evidence and native experiments did **not** establish
improved rescue correctness. See the bench report `planning/CONTRACT-REVIEW-RESULTS-20260925.md`
for preserved failures, request costs and the distinction between live results
and final offline citation-parser repairs.
