# Resource evidence v2 (1.2 development)

The game computes a scoped inventory report in one bounded read. The private
Agent explains it; raw queries remain available for further investigation.

## Use the existing tools

Overview on the current map:

```json
{"resources":{}}
```

Exact steel comparison without paging a dictionary:

```json
{"resources":{"def_names":["Steel"],"scopes":["ground","stored","cached"]}}
```

Include rocks/corpses and other haulable objects:

```json
{"resources":{"focus":"all_haulable"}}
```

These are arguments to `get_game_info`. Do not mix `resources` with `requests`
in one call. Legacy object templates, `query_game` GraphQL and schema discovery
remain unchanged. The Mod's catalog supplies all resource parameter constraints.

## Reading the report

| Field | Meaning |
| --- | --- |
| `selection` | Map, definitions, focus, requested scopes and ground visibility filter |
| `sample` | Colony/load/tick/sequence; report scopes share this sample |
| `coverage` | Complete/partial/unavailable separately for each requested scope; records excluded by the selector are explicit |
| `rows` | One row per selected definition, using native labels and IDs |
| `rows[].scopes.<scope>.units` | Exact physical units only when that scope completed; otherwise null. Cached units describe a directly observed entry |
| `rows[].scopes.<scope>.status` | Physical scope complete/partial/unavailable, next to its quantity |
| `rows[].scopes.<scope>.known_units` | Observed physical lower bound, not a global total |
| `rows[].scopes.<scope>.item_count` | Exact distinct physical IDs in a complete scope, not the number of displayed examples |
| `rows[].scopes.<scope>.fog_filter` | `unfogged_cells` for filtered ground; `none` for the other scopes |
| `rows[].scopes.cached.entry_present` | True for an observed key, false when the completed cache scan contains no key, null when unread; cached scope is omitted when not requested |
| `selected_physical_union` | Game-computed ID union of requested physical scopes, excluding cached values; not an all-colony total |
| `overlaps` | Shared item IDs between scopes, including storage/ground overlap |
| `item_examples` | Bounded examples for drilldown; not an exhaustive list unless `examples_complete` says so |
| `row_page` | `total_rows`/`returned_rows` count selected definition rows across scopes, not ground categories; pagination is distinct from read completeness |

`ground` is spawned, non-destroyed HaulableEver map items, default excluding
fog. `stored` retains native SlotGroup.HeldThings irrespective of fog: storage
cells are not arbitrary containers, and storage need not be a subset of
*visible* ground. Inventory, worn, equipment and carried are separate scopes
for spawned free colonists on the selected map, not caravans or prisoners.

`cached` reads ResourceCounter without forcing a refresh. An observed cached
entry can be exact even if later unrelated keys failed. It is not filtered by
fog and does not establish why the native counter differs from physical counts.
Do not invent Forbidden rules, refresh timing or building absence.

The default supplies view selects CountAsResource definitions, weapons, and
Apparel category descendants. It is a stated resource view, not every map
object. Exact definitions bypass that focus filter, so querying ChunkGranite
still works. Zero-only cached definitions are hidden in an overview; request
an exact definition when its presence/zero matters.

## Limits and partial results

- Existing 12ms game-read budget remains; reads stay on the game thread.
- Each scope is bounded to 8192 yielded item records; exact definitions allow
  targeted reads instead of broad discovery.
- At most32 exact definitions,64 returned definition rows and20 examples per
  row. Default view is32 rows with3 examples per definition.
- Oversized reports are rejected; no silently chopped JSON is returned.
- If a physical scope fails, its exact quantity and union are not fabricated.
- Conflicting identity/quantity invalidates the report instead of publishing
  contradictory totals.
- Unknown exact definitions fail the batch before quantity reads; each invalid
  input index is reported in the existing issues array. Discover names through
  the existing thing_defs target; a rejected or omitted definition is not zero.
- Later row/detail requests are fresh samples. Do not pretend pages from
  different ticks are one atomic inventory.

Other maps, arbitrary nested containers, corpse/minified inner contents,
unmined ore and embedded building materials are outside this report's scope.
Use supported precise queries when more facts are needed; unsupported data
must remain unavailable.

## Upgrade and deployment

This is a development candidate, not a released 1.2 package. The Mod publishes
`resource_evidence.v2`; the current plugin accepts both v1 and v2 catalogs.
V2 replaces the parallel `quantities`/`known_quantities`/`item_counts` maps and
`cached_entry_present` with scoped readings, and replaces `view` with `row_page`.
Do not interpret v2 as v1 or expose both shapes as duplicate fields. Wire-v2
authentication, pairing token and model configuration are unchanged. A previous
plugin that only accepts v1 falls back to the existing object/GraphQL queries
when connected to this Mod; update the plugin to use the v2 resource mode.

Native medicine discovery uses the existing template target:

```json
{"requests":[{"target":{"type":"thing_defs","is_medicine":true,"first":20},"sections":["basic"]}]}
```

The GraphQL equivalent is `thing_defs(isMedicine:true)`. This filters native
`ThingDef.IsMedicine`, independently of names. False selects non-medicine;
omission applies no filter. Other selectors intersect it and cursors remain
filter-bound. Complete all required pages before claiming category coverage,
then use returned exact definitions for quantities. Classification does not
establish stock quantity, quality, treatment effect or pawn usability.

Do not replace loaded game DLLs while RimWorld is running. The release build
and validation artifacts are staged separately; restart the game and reload
the plugin after updating. No new gameplay actions, background model requests
or autonomous colony management are introduced.
