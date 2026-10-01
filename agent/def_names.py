"""Deterministic Def-name recovery backed by the local Def knowledge index.

Ported from rimworld-bench `experiments/feasibility_20261001/audit.py`
(`tokens` / `catalog_audit`), which was verified against the real
3415-definition catalog: every canonical name round-trips to itself;
`IndustrialComponent` resolves uniquely to `ComponentIndustrial`; the
225 pre-existing exact-name ambiguity groups remain the only collision
groups; an injected collision and an incomplete catalog both refuse
auto-selection.

This is a narrow lexical mechanism, not semantic synonym resolution.
Native reads stay the arbiter of existence and quantity: normalization
only rewrites the requested name string before the ordinary exact
lookup, and a still-missing name returns names-only discovery
candidates — never computed values and never an automatic replacement.
Ambiguity and incompleteness refuse auto-selection exactly like the
native identity contract does.

Opt-in with `NEKO_DEF_NAME_RECOVERY=1`; unset keeps every lookup
byte-identical to the previous behavior.
"""
from __future__ import annotations

import os
import re
import sqlite3
import unicodedata

MAX_CANDIDATES = 8

DISCOVERY_MEANING = (
    'Definition-name discovery matches from the local Def knowledge index; '
    'names only, not computed values or automatic replacements. '
    'The index may lag the loaded game; an exact definition lookup stays the arbiter.'
)

THING_DEF_TYPES = ('ThingDef',)


def recovery_enabled():
    value = os.environ.get('NEKO_DEF_NAME_RECOVERY', '').strip().lower()
    if value not in {'', '0', 'false', 'off', '1', 'true', 'on'}:
        raise ValueError('NEKO_DEF_NAME_RECOVERY must be a boolean')
    return value in {'1', 'true', 'on'}


def tokens(text):
    """Word-order/size-insensitive token key. Ported unchanged from the audit."""
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    text = re.sub(r'([A-Z])([A-Z][a-z])', r'\1 \2', text)
    return tuple(sorted(re.findall(r'[^\W_]+', text.casefold())))


def _thing_def_rows(view):
    """(canonical defName, published names) for ThingDef-family index rows.

    Index keys are `<Type.FullName>/<defName>`; the canonical spelling is
    retained by the key even when the view casefolds published names.
    """
    rows = []
    for row in view.search_rows():
        key = row.get('key') if isinstance(row, dict) else None
        if not isinstance(key, str) or '/' not in key:
            continue
        type_name, def_name = key.rsplit('/', 1)
        base = type_name.rsplit('.', 1)[-1]
        if base not in THING_DEF_TYPES or not def_name:
            continue
        names = row.get('names') if isinstance(row.get('names'), list) else []
        rows.append((def_name, [n for n in names if isinstance(n, str) and n]))
    return rows


class DefNameResolver:
    """Word-order recovery plus names-only discovery over one frozen view."""

    def __init__(self, rows, *, complete, source):
        self.complete = bool(complete)
        self.source = source
        self._exact: dict[str, set[str]] = {}
        self._normalized: dict[tuple[str, ...], set[str]] = {}
        for def_name, names in rows:
            for text in (def_name, *names):
                self._exact.setdefault(text.casefold(), set()).add(def_name)
                self._normalized.setdefault(tokens(text), set()).add(def_name)

    @classmethod
    def from_knowledge_view(cls, view):
        metadata = getattr(view, 'metadata', None)
        if not isinstance(metadata, dict):
            raise ValueError('knowledge view has no snapshot metadata')
        complete = metadata.get('complete') is True and not metadata.get('issues')
        return cls(_thing_def_rows(view), complete=complete,
                   source={'kind': 'def_knowledge_index',
                           'sha256': metadata.get('sha256'),
                           'generated_at': metadata.get('generated_at'),
                           'complete': complete})

    @classmethod
    def try_from_knowledge_view(cls, view):
        """Build the resolver, or degrade to None on any index-side failure.

        Recovery is an affordance over ordinary lookups; a changed payload, a
        missing file or a drifted schema must never become a lookup failure.
        """
        try:
            return cls.from_knowledge_view(view)
        except (AttributeError, KeyError, TypeError, ValueError, OSError, sqlite3.Error):
            return None

    def normalize(self, term):
        """Audit resolve() semantics, restricted to not-exact-known terms.

        A term already exact-known (canonical defName or published label)
        is left to the native exact lookup untouched.
        """
        if not isinstance(term, str) or not term:
            return {'state': 'invalid', 'candidates': [], 'auto_selection': None}
        if term.casefold() in self._exact:
            return {'state': 'known', 'candidates': [], 'auto_selection': None}
        if not self.complete:
            return {'state': 'incomplete', 'candidates': [], 'auto_selection': None}
        matches = sorted(self._normalized.get(tokens(term), ()))
        state = 'missing' if not matches else 'ambiguous' if len(matches) > 1 else 'unique'
        return {'state': state, 'candidates': matches,
                'auto_selection': matches[0] if state == 'unique' else None}

    def discovery(self, *terms):
        """Names-only candidates for terms native reported missing.

        Two bounded lexical families, never auto-selecting:
        normalized-token collisions (all members are listed) and label/name
        containment in either direction, which covers localized colloquial
        names such as a query containing a published label.
        """
        found: set[str] = set()
        for term in terms:
            if not isinstance(term, str) or len(term.strip()) < 2:
                continue
            needle = term.strip()
            folded = needle.casefold()
            found.update(self._normalized.get(tokens(needle), ()))
            for name, owners in self._exact.items():
                if len(name) < 2 or name == folded:
                    continue
                if name in folded or folded in name:
                    found.update(owners)
        candidates = sorted(found)
        if not candidates:
            return None
        return {'definition_kind': 'ThingDef',
                'matches': [{'definition': name} for name in candidates[:MAX_CANDIDATES]],
                'truncated': len(candidates) > MAX_CANDIDATES,
                'requested': [t for t in terms if isinstance(t, str) and t],
                'source': dict(self.source), 'meaning': DISCOVERY_MEANING}
