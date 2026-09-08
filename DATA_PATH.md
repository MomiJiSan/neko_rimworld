# Object sections and native Thing data path (Mod 0.6.0 / plugin 0.8.0)

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

`submit_response(full_text, agent_text)` retains the natural-language handoff.
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
without being partial. Field failures clear selected advancing endCursor values,
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
