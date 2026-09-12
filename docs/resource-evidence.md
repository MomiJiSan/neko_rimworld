# Resource evidence v3 and shared result references (v1.3 development)

This is the current development contract, not a released package or an Agent
quality result. The Mod computes game facts; the Agent selects scope, evidence
and interpretation. Runtime presentation does not become game reasoning.

## Read resources

The candidate exposes read_resources with the Mod catalog's direct parameters:

```json
{"def_names":["Steel"],"scopes":["ground","stored","cached"],"include_total":true}
```

The cached counter is separate; it never contributes to the physical total.
include_total defaults false. When requested, totals covers the entire request
selection, independently of row pagination. It uses the same ID ledger and
completeness rules as per-definition readings. Cached-only totals are rejected.

There is no focus preset and no purpose-based default. Exact names and shared
conditions intersect. For example, this is an ordinary Boolean field condition:

```json
{"definition_where":{"fields":{"CountAsResource":{"eq":true}}},"scopes":["ground","inventory"]}
```

Other Boolean, numeric, text, nullable, object, collection and possible-type
fields use the same compiler. IsMedicine has no dedicated argument or branch.
instance_where uses public Thing fields; cached has definition counts but no
Thing instance, so combining it with instance_where is a request type error.

Collection-producing GraphQL fields and corresponding template targets accept
where against their element type. Filtering occurs before paging. Cursor
identity includes the full condition. Use describe_game_schema for actual
fields and arguments; selection_contract documents the operator grammar:

- fields maps public member names to conditions; all/any/not compose them.
- Boolean: eq/ne. Numeric: eq/ne/in/gt/gte/lt/lte. String/ID:
  eq/ne/in/contains/starts_with/ends_with; ordinal, case-sensitive by default.
- Enum: all declared values. Nullable: is_null, distinct from failed reads.
- Collections: some/every/none over the underlying source. Empty every/none
  are true; a null collection is not an empty collection.
- Field arguments use the existing public schema and defaults. They cannot
  turn predicate input into a single display page.
- types selects public interface/union alternatives. reach traverses a declared
  same-type reference/collection, such as a category parent, with cycle/budget
  bounds. String keys are not guessed into private object relationships.

Unknown reads use three-valued logic. Unknown membership cannot silently
become false, an empty collection or an exact zero. Limits apply uniformly,
not by topic. New scalar families must have an implementation before publishing
a partly supported condition interface.

## Reading quantities and scope

| Field | Meaning |
|---|---|
| selection | Requested definitions/conditions, scopes and ground visibility |
| sample | Colony/load/tick/sequence for this one observation |
| coverage | Each scope's completion and scan diagnostics |
| rows | Per-definition readings across selected scopes |
| scopes.*.units | Exact complete physical quantity; otherwise null. Cached reads refer to an observed entry |
| known_units | Known lower bound, not the whole-colony quantity |
| item_count | Distinct physical records; not stack units or displayed example count |
| fog_filter | Scope-specific visibility rule |
| cached.entry_present | Observed key, proven absence, or unknown; absent is not zero |
| selected_physical_union | ID union of requested physical scopes, excluding cached |
| overlaps | Observed shared IDs; an incomplete scan cannot establish the full intersection |
| row_page | Displayed definition rows and continuation, separate from source completeness |
| totals | Optional whole-request physical scope quantities and ID union |

Ground contains spawned non-destroyed HaulableEver items and includes storage
cells, while visible_only filters ground by fog. Stored reads SlotGroup cells
regardless of fog. Inventory, worn, equipment and carried cover spawned free
colonists on the selected map. Cached reads do not refresh the game counter.
These scopes neither count facilities nor explain why counters differ.
Unrequested stored detail does not imply ground excluded storage-cell items.

Examples are bounded drilldown evidence. They are not an exhaustive inventory
unless their separate completeness flag supports that interpretation. Maps,
caravans/prisoners, arbitrary nested holders and inner corpse/minified contents
outside the declared scope remain outside it. Precise supported queries remain
available for unresolved facts; no tool order or business phase is imposed.

## Compose the answer itself

```json
{
  "answer":[
    {"observation_id":"ab19de34b557","paths":["/rows/0/scopes/ground/units","/rows/0/scopes/ground/item_count"]},
    {"text":"单位数和物品记录数含义不同。"}
  ],
  "language":"zh"
}
```

Use an actual ID from the current run. Paths address its data, optionally with
a leading slash, not the response/payload envelope. Actual array indices are
required; internal ~2 description patterns are not user paths. Reference parts
emit fact text themselves. Do not append them to a second textual copy of the
same facts. A text-only answer uses one text part; detail is optional extra
explanation. One answer composition supplies both full text and the short
handoff. All free statements remain subject to correctness review.

The published observed_values.v1 description works for all public query result
types. Canonical field names come from actual executed resolvers, not aliases
or the last array item. Effective arguments, subjects, windows and read status
remain attached, including for empty collections. Identical descriptions may
share storage; differing polymorphic meanings keep concrete path overrides.
Scalar references retain the required measurement context. Stack units and
item records are different measurements even when their paths are nested.
Exact duplicate/bound references render once, without fuzzy identity matching
or cross-observation calculations. Display labels are escaped as literal data.

The answer accepts 1..16 parts and at most four distinct observations. Each
reference group accepts up to32 paths. Combined answer must fit180 tokens;
full text plus detail must fit12000 characters. Budget feedback identifies fact
cost and remaining text space, without deleting required scope or unknowns.
Valid inputs survive a rejection. Changing answer requires confirming existing
detail or clearing it with null. Pending dependencies never enter fallback text.

## Bounds, provenance and compatibility

The12ms game-read budget,8192 records per resource scope,32 exact definitions,
64 returned rows and20 examples per row remain. Oversized results and identity
conflicts fail explicitly. Unknown exact names are reported before quantity
reads. Later pages are fresh samples, not one atomic observation.

Compact observation references retain full request, connection and sample
identity in their envelopes. Saved reports preserve exact structural path keys
and prioritize referenced observations; insufficient storage reports unconfirmed
saving. Historical reports are not regenerated under new semantics.

New catalogs advertise resource_evidence.v3, schema_conditions.v1 and
observed_values.v1. Wire v2 authentication is unchanged. With older catalogs,
the plugin retains their actual query capabilities and uses text-only answer
parts when result descriptions are absent. Old v1/v2 resource formats and
historical reports remain readable; old focus/isMedicine inputs are not aliases
in the v3 catalog. Game DLL activation still requires a normal game restart.
