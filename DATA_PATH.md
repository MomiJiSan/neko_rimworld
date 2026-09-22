# Direct collection query revision (public_query.v2)

A single query can be sent directly as {from,args,where,select,...}; requests:[...] batches the identical shape under one sample. Mixing the two forms is rejected.

The current Agent interface exposes Pawn.inventory, equipment, apparel and carried directly as Thing collections. Their populations remain distinct; selection, conditions, paging and aggregates are identical. Tracker and inner-container fields remain in the developer GraphQL schema, not the Agent field directory. Thing.contents explicitly reads a container through the same rule. Null intermediate containers remain null; a null final carried Thing is an observed empty collection.

Grouped queries may select values outside group_by. The executor verifies that requested values are shared within the group and returns them in rows.values. Shared object identities avoid repeated projection. Different values return null with group_field_varies, never an arbitrary representative. When enumeration is incomplete, shared values are unknown. Only displayed groups project their fields; all matching objects still contribute to complete aggregates.

Example (ordinary collection operations, not an inventory preset):

```json
{"requests":[{"from":"things","args":{"map_id":"current","include_held":false},"where":{"definition.category":{"eq":"Item"}},"group_by":["definition.def_name"],"select":{"definition":["def_name","label","is_weapon","is_ingestible"]},"aggregate":{"quantity":{"op":"sum","field":"stack_count"}}},{"from":"spawned_free_colonists","args":{"map_id":"current"},"select":["id","name","inventory","equipment","apparel","carried"]}]}
```

The v1.4.2 integration keeps v2. Selection arrays can mix string paths and
field maps, for example `['id', {'definition': ['def_name', 'label']}]` (shown as
JavaScript notation). A bare relation map is a child selection only if every
key is an actual child field and none is `select/args/where/aggregate/group_by/
order_by/limit/offset/on`. Otherwise it must obey the explicit options grammar.
Duplicate/overlapping fields, unknown names and misplaced parameters are
rejected; arguments and filters are never silently relocated. Compiler errors
include their input path and owning type where available; some condition and
root-envelope errors still identify only a containing object or message.

The common-field descriptions and additional `field_notes` are generated from
the same registry used by `describe_query`. Notes carry existing nonempty
field descriptions; this is not a claim of complete units or enum metadata
for every field. Text submission uses only `{text}` parts, top-level language,
and the existing answer/detail contract in both ordinary calls and fallback.

This revision changes the canonical shape, so it declares v2. Saved v1 observations are not rewritten. The plugin can connect to either advertised version. Native identity and scope rules below remain valid; v1 tracker traversal examples below are historical.

The v1.6 query/execution revision keeps v2. Root argument branches are generated
from the same registered arguments. Definition predicates may be safely projected
through all/any/not before Thing enumeration; mixed or unknown branches retain
three-valued behavior. Instance-only filters and display pagination do not promise
to lower source candidate scans. Root and nested collection scopes retain actual
arguments and their own where; shared relation descriptions are returned once.

The runtime enforces a research deadline and a submission-only finalization phase.
The experimental working-data view and task-goal wording were tested in production
code, then withdrawn from defaults after inconclusive correctness confirmation.
Append-only observations and audit remain the authoritative record.

---

# Shared v1.4 query data path

The current Mod advertises `public_query.v2` (the initial v1.4 revision used v1). The plugin exposes `query` and `describe_query`; it does not simultaneously advertise legacy templates, GraphQL or the separate resource tool. Older connected Mods retain their existing adapter. Legacy transports below are retained for compatibility/development, not a second recommended Agent workflow.

When advertised by the connected Mod, `spatial_query.v1` is exposed by the
semantic plugin as `inspect_space(from,to)`. This is a single current-tick,
read-only native reachability check from one Pawn to one Thing or map cell.
References accept exact names or IDs directly; ambiguity returns candidates and
never silently selects one. The operation does not infer WorkGiver eligibility,
reservation, route distance, Room/Region membership or future job success.

## Single executable field vocabulary

`PublicFields` derives canonical snake_case names, native mappings, types, arguments and operations from the executable Mod schema. `def` becomes `definition`; `def_name`, instance `id`, and display `label` remain distinct. Python forwards requests without renaming game fields or maintaining classification tables. Public conditions translate to the same typed condition evaluator used by existing native reads. Internal `fields/types/arguments/match` aliases are rejected.

With the v1.6.1 Mod, single-object conditions accept a pure child-field map or
an explicit `where` wrapper. Collections still require `some/every/none`.
Concrete `on` selections accept the same field arrays/maps and explicit selection
options as related objects. Structural discriminator errors are reported before
dependent field errors. Batch compilation reports one primary error per invalid
root request and lists indices that passed validation; the entire batch remains
unexecuted if any request is invalid. These diagnostics reach the model in the
existing error message and issues. They do not represent successful game reads.

The Mod directory now also supplies `value_domains` for all ten exposed native
enum types. Each field points to its own domain; `describe_query` accepts either
an object type or the full native domain name (for example `Verse.ThingCategory`).
The plugin forwards this metadata without a separate enum table. Wire values
remain strings. Enum conditions accept `eq/ne/in/is_null`; native flags additionally
accept comma-separated declared names and `has_all/has_any`. Default matching is
case-sensitive; `case_sensitive:false` permits ordinal case folding. Invalid
literals fail before reads, while unknown observed native values retain unknown
membership. Names, labels and Def identifiers remain open strings. A valid empty
query is still zero; no matching record cannot establish an arbitrary string as a
valid category.

`query({requests:[...]})` selects a root, its `args`, optional `where`, and fields in `select`. Related objects are selected directly; collection transport `items/nodes` windows are not part of selection paths (a native collection actually named `items` remains a field). Queries can include counts, declared additive sums, min/max and grouping. Filtered detail and aggregates use the same candidate collection; aggregates precede display `limit/offset`. Selected objects return scalar fields and parameter-free collections by default; wrapper-only objects expand their related fields, so selecting inventory does not return a misleading empty object. Long descriptions and argument-dependent scans remain explicit. Objects accept projection options; collection-only options on single objects or concrete projection branches are rejected before any game read.

```json
{"requests":[{"from":"things","args":{"map_id":"Map_0","visible_only":false},"where":{"definition.category":{"eq":"Item"}},"group_by":["definition.def_name","definition.label"],"aggregate":{"quantity":{"op":"sum","field":"stack_count"},"records":{"op":"count"}}}]}
```

This is an example of an explicit native category filter, not a hidden supplies preset. `IsWeapon` includes improvised weapons; `CountAsResource` is readout membership, not ownership or usefulness.

## Populations and relationships

- `things`: selected loaded maps, spawned indexed Things and optionally their declared holder trees; default `include_held:true`, `include_unreal:false`, `visible_only:true`. It is not all world/caravan possessions or proof of player ownership.
- `pawns`: all reachable pawns on selected loaded maps, using the native Pawn index and holder trees. `spawned_free_colonists` retains its narrower explicitly named population.
- `pawn(id)`: finds the returned pawn identity across loaded-map pawns.
- Map selectors accept `map_id:"current"` (map root `id:"current"`) and return the resolved ID in scope. This is available across roots with map selectors, without an extra game/maps read.
- `map(id)`: storage groups and the separate original ResourceCounter.
- Native unreal contents follow `ThingOwnerUtility.AreImmediateContentsReal`; excluding a corpse/minified inner object does not exclude all its descendants. Explicit `include_unreal:true` is available for inspection and must not be blindly added to wrapper counts.
- `storage_group` is storage-cell membership, overlapping spawned-map membership; it is not acceptance by current storage settings. `map`/`map_held`, `position`/`position_held`, and holding relations remain distinct.

The game computes physical ID deduplication. Ordinary nonphysical collections retain their entries; grouping keys and independent requests are not assumed to form disjoint populations.

## Results and limits

Each result reports effective scope and predicate, `scan_complete`, `matched_count` (null when incomplete), `known_matches`, `fields_complete` and explicitly typed aggregates. Requested detail includes `rows` and `page`; an aggregate query with `limit:0` omits both, including in nested collections. This means detail was not requested, not that scanning was complete. Consumers must treat these two fields as optional for aggregate-only responses. A partial aggregate has no exact value. A display limit does not reduce an already complete aggregate. Empty complete count/sum is zero; empty min/max is null.

Every call/continuation is a fresh observation; IDs can disappear after stack split/merge. Production reads remain on the game thread with a 12ms read budget. Batches are bounded to 8 requests, depth12/512 selections, 8192 candidates per collection, 20000 projected objects and 80000 result characters. Wider requests can be partial; a frozen 5s reader is not evidence of production timing.

Complete raw observations and execution requests remain in audit storage. The model receives compact data, scope and issues. The new contract uses the existing text-only answer-part composition, not descriptor/path references: answer is the short conclusion, detail carries the full factual report. No new submission parameters were added. Metadata discovery is not a live fact observation. No mutation, automatic pause or background world mirror is added.

## Evaluation

The existing paired version runner seals each plugin and query DLL separately. `bench.py experiment queries` checks fixed queries against native-graph reference values; `experiment versions --plan` runs the actual Agent. Owner-dependent native projections (body parts, capacities, backstories, computed stats) are captured separately for each pawn. Limited numeric evidence adapters validate matching scopes; complex/open answers remain explicit review cases. Delivery and software tests are not semantic quality scores.

## Legacy implementation reference (v1.3 and earlier)

# Object sections and native Thing data path (Mod 0.6.0 / plugin 0.8.0)

Current development (v1.3, unreleased): read_resources forwards the Mod-owned
resource_report v3. Shared where conditions apply at collection producers,
before windows; transport items/nodes are views of those producers. The Mod
publishes selection_contract and result_description_contract alongside its
executable catalog. Exact and template queries share the same implementation.

submit_response composes answer:[{text} | {observation_id, paths?}], optional
detail/language. Values retain Mod-described identity, selection, units, sample
and completeness. Source descriptions are interned without erasing per-element
polymorphic differences. Missing old-Mod capability yields text-only submission,
not invented result descriptions. See docs/resource-evidence.md for the current
API. Historical result formats remain readable; their data is not rewritten.

## Agent-facing tools

`get_game_info(requests)` selects a target object, then one or more of its
available section templates. The Mod exports the complete JSON Schema for these
choices, their constraints, and each section's field selection. Python does not
keep another selector, section enum or gameplay field table.

```json
{
  "requests": [
    {"target": {"type": "colony"}, "sections": ["overview"]},
    {
      "target": {"type": "pawn", "id": "pawn1"},
      "sections": ["status", "mood", "health", "equipment", "profile"]
    }
  ]
}
```

| Target | Available sections | Selection |
| --- | --- | --- |
| `colony` | `overview` | Game and loaded-map overview |
| `colonists` | `summary` | Discover spawned free colonists using optional `ids`, `map_id`, `name`, `first`, `after` |
| `pawn` | `status`, `mood`, `health`, `equipment`, `profile` | A specific pawn using required `id` |

Pawn lookup covers spawned free colonists on loaded maps. It does not cover
caravans, prisoners or all world pawns merely because the type is `Pawn`. Name
filters match case-insensitive substrings and may return multiple people;
filters combine with AND. Use returned IDs for follow-up observations.

Each pawn template includes `id`, `name`, and `mapId`. Multiple sections merge
into one nested pawn result. Repeated equivalent targets share that result;
each input request keeps its own binding. Requests for different objects can
select different sections in the same batch.

`query_game(document, variables?)` freely selects supported fields, nested pages
and roots from the same executable GraphQL schema. A template call is not a
prerequisite. For example, only read one pawn's current mood:

```json
{"document":"query($id:ID!){pawn(id:$id){id name mood{level}}}","variables":{"id":"pawn1"}}
```

Templates cover useful combinations of facts. Precise queries cover other field
combinations and continue nested collection windows. Neither tool chooses the
next investigation step for the agent.

`submit_response(answer, detail?, language?)` derives the natural-language handoff from one ordered answer composition.
Terminal submissions mixed with observation calls receive a recoverable error;
no answer written before seeing those observations is accepted.

## Canonical pawn graph and section semantics

```text
Pawn
  id / name / mapId
  status       current state, activity, position and needs
  mood         level, thresholds, game-computed thought groups, memories
  health       summary, conditions, capacities, body parts
  equipment    Pawn_EquipmentTracker: Primary / AllEquipmentListForReading
  apparel      Pawn_ApparelTracker: WornApparel
  inventory    Pawn_InventoryTracker: innerContainer
  carryTracker Pawn_CarryTracker: CarriedThing / innerContainer
  profile      age/background, traits, skills and related restrictions
  statDefinitions  discover names/labels without calculating values
  stats        explicitly selected derived stats
```

Section templates select fields over this graph. They do not create separate
reports with repeated, independently read facts. Selecting a parent object does
not serialize all children; unselected leaf getters do not run. Fields shared
by sections are selected once. Different argument values cannot silently become
one collection selection.

Important distinctions:

- Food is a pawn need value, not colony food stock or days remaining.
- Health percentage is a summary, not proof that there are no conditions.
- Mood thought groups use the game's grouping and effective offset calculation.
  Raw memories are separate records and must not be summed as if they were all
  current mood contributions.
- Conditions may refer to a body part or apply to the whole body. Body parts
  retain hierarchy and identity; repeated labels are not unique keys.
- Equipment keeps weapons, worn apparel and carried inventory separate. Item
  identity, definitions, material, quality and durability have distinct meanings.
- Traits retain degree and suppression state. Skills retain level, passion and
  availability. Discover property names using `pawn.statDefinitions(search:...)`
  without computing their values. Derived stats require explicit `defNames`
  (1..16); calculating all StatDefs is not an implicit part of profile.
- Thought groups, memories and conditions default to UI-visible records.
  An exact query must explicitly set `visibleOnly:false` to inspect internal
  records; absence from the visible window does not imply absence internally.
- Missing/inapplicable fields are null; failed reads also have issue paths. An
  empty complete collection differs from an unread or incomplete page.

Pawn and thing IDs use game identity. Definition names identify definitions,
not instances. Body-part references are meaningful within their owning pawn and
load. Memory references are explicitly local to one sample and cannot identify
the same memory in a later observation. References connect related objects
without recursively expanding an unbounded graph.

The exported SDL and section descriptions define exact field names, units,
nullability and default windows. The plugin caches the SDL, but injects only a
type/root directory plus common section schemas. describe_game_schema(types,
include_templates?) retrieves precise supported members as metadata on demand.

## Mod-owned discovery and compilation

GameQueryEngine builds one executable GraphQL.NET schema. TemplateCatalog owns
target/section definitions and parameter bindings, never live reads. Templates
are checked against the initialized schema before advertisement.

`query_catalog` returns schema_id, SDL, section descriptions, both tools'
parameter schemas and query rules. CatalogTools loads it before model execution
and caches it per bridge instance and connection epoch. Concurrent loads share
one request. Reconnect clears the cache; existing runs refuse to silently switch
connections. Missing catalogs produce an availability explanation without stale
game tools. Cached schema knowledge is not agent memory.

The compiler validates every target, section and parameter before game access,
groups equivalent targets, unions their selected fields, adds pawn identity,
and produces one variable-bound GraphQL document. The complete document receives
the same validation and shared cost checks as a precise query.

Template output roots use `o0`, `o1`, etc. `execution.bindings` maps each input
index to its target, sections and output root. A colony root contains game/maps;
a pawn root contains one nested Pawn. Repeated target requests can map to one
root. Python does not expand templates, merge outputs, filter fields or rename
result roots.

## Shared execution and wire v2

```text
get_game_info -> template_query -> target/section compilation
                                               |
query_game   -> graphql -----------------------+
                                               |
                         shared validation and combined cost checks
                                               |
                         serial game-thread field resolvers
                                               |
                         detached JSON + issues + execution metadata
```

IGameData separates the engine from Verse for deterministic tests. ReadContext
caches identities and fields during one query, includes child identity and
arguments in cache keys, enforces thread identity, and never survives execution.
Game references do not enter network workers or agent sessions. Looking up an
object may scan identities; it does not read all of those objects' details.

Framing, authentication, correlation, save/load identity and `query.execute.v2`
are unchanged. Operations are `query_catalog`, `describe_schema`, `template_query` and `graphql`.
Upgrade to Mod 0.6.0 and plugin 0.8.0 together. The old `type/args` template form,
flat Colonist fields and `colony_snapshot` are removed.

Responses retain status, optional data object, issues and operational errors.
Generic `execution` metadata includes the expanded document, variables, schema_id
and bindings. Observation retains the complete originating request and response
envelope, including load/sample identity. Python never projects gameplay fields
or silently crops them. Only agent-authored natural language reaches outer NEKO.

## Paging, coverage and failure

Root `colonists` discovery uses stable-ID ordering and a cursor bound to the load
and normalized filters. A new load or changed filter rejects an old cursor;
changing page size is allowed. A successful page can have `hasNextPage=true`
without being partial. Field failures preserve the discovered hasNextPage value
and clear selected advancing endCursor values,
including aliases, so paging cannot silently skip failed people. Retry affected
IDs with fewer fields or restart discovery.

Nested collection windows use `first` and `offset` and return a Slice:

| Field | Meaning |
| --- | --- |
| `items` | Selected records |
| `total` | Collection size observed for this read |
| `offset` | Requested starting offset |
| `returned` | Returned record count |
| `hasMore` | Another normal window remains |
| `nextOffset` | Continuation offset when safe to advance |
| `complete` | Whether this requested window completed |

`complete` describes the requested window, not whether every record in the full
collection was returned. Inspect it with issues and `hasMore`. Only advance using
an advertised continuation; do not compute a new offset after an incomplete
window and skip unread records. Precise queries can request later windows
without rerunning all section templates.

For example, continue the selected pawn's conditions at an advertised offset:

```graphql
query ConditionsPage($id: ID!, $offset: Int!) {
  pawn(id: $id) {
    id
    health {
      conditions(first: 10, offset: $offset) {
        items { id label severity part { key label } }
        total offset returned hasMore nextOffset complete
      }
    }
  }
}
```

Offsets apply to the collection in that sample. Thoughts, conditions or inventory
can change ordering, repeat or disappear between requests; pages are not frozen.
Memory references belong to the new sample. Correlate persistent objects by
supported IDs and keep every observation's load/tick identity. Do not join old
and new load observations into one pawn state.

Invalid documents, targets, sections, arguments and excessive combined cost fail
before reads. Selected nullable-field failures preserve other data with partial
status and path-specific issues. An unavailable pawn returns null. Deadline
exhaustion is incomplete or explicitly failed, never an ordinary empty list.
The data layer does not rank risks or select the most important rows.

## Resource boundaries

- One operation, at most 16,384 document characters, selection depth 12, 512
  fields and combined estimated cost 20,000. Fragments, directives, introspection,
  mutations and subscriptions are rejected in this schema.
- Combined cost counts nested collection fan-out as well as root rows and fields.
  One pawn containing several collections is not treated as one cheap scalar.
- Root discovery uses `first=1..50` (default 20), ID lists at most 50 and bounded
  identity scans. Nested windows use `first=1..100` (default 20) and
  `offset=0..8192`. The catalog supplies authoritative current limits.
- Section windows usually select 20 rows; skill profiles select 32, backstories
  select 2 and current job targets select 3. These are useful defaults, not
  completeness guarantees; inspect Slice coverage and continue when necessary.
- A 12 ms cooperative read deadline starts with first game access. Parsing,
  validation and serialization add overhead; this is not a hard frame guarantee.
  Resolvers stay synchronous on the game thread, one request per Update.
- One template batch shares one sample and total budget, even with five sections.
  Oversize JSON or tool output returns an explicit error instead of cropping.
- Independent requests/pages do not imply frozen state, long-term model memory
  or cross-run Verse caches.
- Protocol JSON depth is bounded at 32; audit depth allows 40 to retain that
  nested payload plus observation/tool wrappers while keeping its own limit.

## Extension and validation

Add a field's Mod schema and getter; add a section selection when a reusable
combination is useful. New collections declare bounded windows, coverage and
cost. The catalog and schema_id update from the same definitions. Python bridge,
business DTOs, agent loop and handoff need no domain branches.

BridgeSmoke uses the production schema/compiler with an IGameData fixture. It
checks section union, template/exact equivalence, selected getter counts, nested
limits/partials, invalid-batch zero reads, combined cost, stable root pagination,
stale cursors and calling-thread identity. Export actual C# fixtures using:

```powershell
$env:NEKO_QUERY_CATALOG = '<absolute path>/tests/fixtures/query_catalog.json'
$env:NEKO_TEMPLATE_RESULT = '<absolute path>/tests/fixtures/template_result.json'
```

Python tests consume these results to verify catalog tools, intact nested data,
one batch/observation, original requests, cache/reconnect, Hosted probe shape and
submission isolation. Deploy the complete Assemblies directory including GraphQL
and transitive DLLs. .NET tests do not replace RimWorld/Mono and live-game checks.


## Native objects (Mod 0.6.0 / plugin 0.8.0)

Additional targets in get_game_info (all descriptions and selections are Mod-owned):

| target | selector | sections |
| --- | --- | --- |
| things | required map_id; request_group, def_names, name, visible_only, first, after | basic, placement, components |
| thing | required id; optional map_id | basic, placement, components, contents |
| thing_defs | name, category_def_name, first, after | basic, hauling, ingestible, component_definitions |
| thing_def | required def_name | same definition sections |
| map | required id | storage_groups, resource_counter |

Exact roots use mapId, requestGroup, defNames, visibleOnly, categoryDefName,
defName rather than their template selector spellings. Native member names keep
case: stackCount, Stuff, HitPoints, MapHeld, AllComps, etc. id/runtimeType/ref,
collection windows and stat value/valueText are explicitly described projections.
Every collection wrapper retains its own coverage; templates do not manufacture
additional domain objects. New things/thing_defs pages use nodes + pageInfo.
Nested native collections reuse the existing items/total/offset/returned/hasMore/
nextOffset/complete wrapper. AllComps belongs to ThingWithComps instances;
ThingDef.comps contains CompProperties definitions. They are not interchangeable.

Example:

```json
{"requests":[{"target":{"type":"things","map_id":"map1","request_group":"HaulableEver","first":10},"sections":["basic","placement"]}]}
```

Thing is now shared by map queries and pawn.equipment.AllEquipmentListForReading,
pawn.apparel.WornApparel, pawn.inventory.innerContainer and
pawn.carryTracker.CarriedThing. The former flattened Item/count/stuffDefName/
quality and equipment.primaryItemId/equipped/apparel/inventory schema is removed.
Definitions and materials remain ThingDef references. InnerThing and InnerPawn
remain detached special-object references. Holder refs are observation-scoped;
Thing IDs are not a guarantee of identity through stack splitting/merging.

Thing.AllComps exposes a ThingComp interface with bounded inline fragments:

```graphql
{ thing(id:"thing123") {
  id def { defName } stackCount
  AllComps(first:10) {
    items {
      ref runtimeType supported supportedType
      ... on CompQuality { Quality }
      ... on CompRottable { RotProgress Stage TicksUntilRotAtCurrentTemp }
    }
    total hasMore nextOffset complete
  }
} }
```

Supported base projections: CompQuality, CompRottable, CompForbiddable and
CompIngredients. Subclasses preserve runtimeType and use supported base members;
unknown components appear as UnsupportedThingComp metadata, with supported=false.
Multiple components of the same class keep separate per-sample refs. No arbitrary
reflection getters, jobs, hauling actions, reservation mutations or writes are exposed.
Inline type conditions participate in validation, template merging, depth/field/
cost checks and partial-page repair. Named fragments and directives remain rejected.

The things root discovery is spawned objects only; default visible_only=true omits
fogged candidates. Native HaulableEver/HaulableAlways/HaulableEverOrMinifiable/
Everything groups describe candidates, never current pawn-specific permissions.
Direct thing lookup searches loaded maps then IThingHolder descendants, with
reference-cycle detection, depth/count limits and the existing read deadline.
No live handles are retained across requests. Very broad scans can fail with an
explicit deadline/scan error: a small returned page is not a promise of cheap
whole-map scanning. Selection keeps at most first+1 candidate nodes and reads
only identity/filter fields before selecting detail fields. No global total or
frozen snapshot is claimed by those pages.

SlotGroup.HeldThings and map things overlap. StorageParent describes ISlotGroupParent,
which is not necessarily IThingHolder. ResourceCounter reads the game's cached
stored-resource count dictionary without forcing updates; it is not a full-map
inventory or a count of all held items. ThingFilter.AllowedThingDefs reports
allowed definitions only, not the complete instance acceptance decision.

Schema discovery results are metadata mappings, never live Observation evidence.
The directory is tied to schema_id and connection epoch; exact describe replies
are cached per tool setup. Missing directory on an older Mod prompts an upgrade.
The cache limit is 256k characters, distinct from the model's compact directory.

Map list and map-reference fields now match and share their summary reader. Upward ownership/storage references default to identity/summary rather than implicit contents; explicit selection still permits contents. IThingHolder.parent is a native relation, not an invented ownership classifier. The compact field index covers every type reachable through current public roots; discovered metadata remains available for detailed semantics.
