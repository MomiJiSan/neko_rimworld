"""One opt-in, ordinary-JSON context path. Historical codecs are not runtime modes."""
from __future__ import annotations

import hashlib
import json
import os

MAX_SOURCE_CHARS = 4 * 1024 * 1024
CONTEXT_TOOLS = frozenset({
    'look', 'count', 'inspect_space', 'inspect_route', 'survey_space',
    'inspect_state', 'evaluate_activity', 'execute_python', 'read_result',
})


def enabled():
    if os.environ.get('NEKO_EVIDENCE_COMPACT', '').strip():
        raise ValueError('NEKO_EVIDENCE_COMPACT is retired; replay with frozen historical source. '
                         'The current opt-in is NEKO_CONTEXT_PRUNING=1.')
    value = os.environ.get('NEKO_CONTEXT_PRUNING', '').strip().lower()
    if value not in {'', '0', 'false', 'off', '1', 'true', 'on'}:
        raise ValueError('NEKO_CONTEXT_PRUNING must be a boolean; numbered encoding modes are retired')
    return value in {'1', 'true', 'on'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def make_pruner():
    from .context_pruning import ContextPruner
    return ContextPruner()


def model_size(value, *, arguments=None):
    """Standalone size, independent of earlier messages and note suppression."""
    return make_pruner().compile(value, arguments=arguments).view_chars if enabled() else len(canonical(value))
