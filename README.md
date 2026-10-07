# N.E.K.O RimWorld

Read-only colony consultation through the local RimWorld Mod and an independent model.
The host agent sees `rimworld_consult(question)`. No game orders or mutations are executed.

## Deployed default (2026-10-07)

The selected baseline is `v3-subtraction30-20261007-v3`: semantic reads, ordinary-JSON request-scoped pruning, activity composition and deterministic Def-name recovery when supported. There are no deployment switches selecting old input or query strategies. Public live delivery uses text; the delivery critic is retired.

The original30 run delivered30/30 with84 requests and881,860 public input tokens. Its evidence adjudication was18 clean/12 minor/0 major/0 fatal. Selecting this default does not establish v2.4 quality parity or a new quality result. Fixed-evidence first-draft ablations are experiments, not deployed behavior.

The look/count interface is mandatory: missing or uncompiled public query directories stop consultation before model dispatch. Historical reports use the same interface. Completion requires the explicit `submit_response` tool; ordinary assistant text is progress only. The runtime no longer accepts `allow_text_completion` or `prepared_calls`. Caller-prepared sessions remain supported through `execute(session, settings)`. Legacy template/GraphQL/resource tool publication has been removed; the internal public query backend remains. Unavailable knowledge uses exact native names; supported world contracts retain their own state projection. Production never imports archived experiments.

## Available reads

With native world support, the model sees `look`, `count`, `survey_space`, `evaluate_activity`, `inspect_actions`, `inspect_threats`, available `wiki`, and `submit_response`. Actual tools depend on negotiated capabilities and ready indexes. The activity tool replaces overlapping public path tools while retaining their native readers internally.

- `look`: explicit aspects; physical `state` does not include Pawn health/skills/work/gear. `focus` selects skills/work/capacities/state facets; `stats` requests exact StatDefs; `include=[body_parts]` retains full anatomy detail.
- `count`: records or stack units, grouping and bounded examples. Holder membership is not ownership.
- `survey_space`: objects/environment/geometry selection and pagination. Omitted include retains full survey; geometric inclusion is not reachability.
- `evaluate_activity`: move, transfer or state, actors×destinations, optional approach, destination state, environment, objects and geometry. Only requested actor facts are read. actor travels/interacts; user receives; subject supplies the transfer origin. There is no separate multi-activity batch option.
- `inspect_actions` checks audited native player-order conditions without executing them. `inspect_threats` separates hostility, active classification and incoming attack checks.
- `wiki` combines local definitions and version-bound implementation lookup (`source:`). Source knowledge is not current game-state evidence.

Ordinary JSON retains sample, actor/user/stage/scope, unknown and partial states. Same-read identities and equal parent fields may inherit; environment_shared has local overrides and preserves path/corridor separation. Full native observations remain in the audit. The diagnostics include escape route is not supported.

## Configuration and migration

Keep model endpoint/name/key, deadlines, output length, temperature and pairing configuration in `config.example.toml`. The model is user-configured; the experiment model is not hard-coded.

Remove `NEKO_CONTEXT_PRUNING`, `NEKO_ACTIVITY`, `NEKO_DEF_NAME_RECOVERY` and `NEKO_PUBLIC_REFERENCES`. Matching former baseline values produce migration notices only; contrary/invalid variants are rejected, not silently mapped. `NEKO_EVIDENCE_COMPACT` and production `NEKO_PUBLIC_INPUT_TOKEN_LIMIT` are retired. Bench retains explicitly declared per-case input budgets outside production.

Remove `review_before_delivery`. A stored false value is tolerated only for migration; true is rejected. Formal content review remains solely `bench.py evaluate review`, outside the consultation runtime.

`NEKO_RIMWORLD_KNOWLEDGE_DIR` and `NEKO_RIMWORLD_MECHANISMS_DIR` remain optional path overrides. Definition recovery requires a ready complete index and refuses ambiguous matches; native lookup determines existence.

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

## Work, delivery and audit

Valid caller/colony identity binds persistent work; missing identity or retained work falls back to a standalone consultation. Turns for the same role and colony serialize. History is attached as historical evidence; workspace_history is published only for existing history/checkpoints or an explicit report ID. Reports and exchanges save automatically. No autonomous observation or learning is implied.

A failed submission retains valid draft fields. Pure short-answer length repair sends answer only and preserves detail; other invalid claims keep research evidence. Research closes near the deadline and only submission remains. preferred_cycles is guidance, not a hard model-call limit. Existing deadlines and tool/result size bounds remain.

Live text answer is limited to180 tokens and answer+detail to12000 characters. Full reports, raw evidence, failures, usage and incomplete status remain visible. Historical reference parts still resolve; removing the live reference option does not invalidate saved reports.

## Development and preserved research

Run plugin pytest with UTF-8 in both parent and child processes. Software checks and fixed input equivalence do not prove content quality. Windows-only/platform failures are recorded separately.

Retired execution policy, delivery review, production input-budget code and old configuration studies are under `rimworld-bench/experiments/runtime_options_20261007/`. The previous README is `plugin-README-before.md` there. Original experiments, questions, answers, failures and frozen runners stay unchanged. Reproduce old variants with their frozen source, never with current defaults pretending to be an old mode.
