"""Working notes are sourced handoffs, not new game facts or learned rules."""

import copy


def checkpoint_note(note, *, current_turn=None):
    if not isinstance(note, dict) or set(note) - {"work_note", "open_questions", "anchors"}:
        raise ValueError("Use work_note, open_questions and anchors")
    text = note.get("work_note")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("work_note requires 1..4000 characters")
    questions = note.get("open_questions", [])
    if (
        not isinstance(questions, list)
        or len(questions) > 8
        or any(not isinstance(q, str) or not q.strip() or len(q) > 500 for q in questions)
    ):
        raise ValueError("Use up to eight nonempty open questions of at most 500 characters")
    anchors = note.get("anchors", [])
    if not isinstance(anchors, list) or len(anchors) > 8:
        raise ValueError("Use up to eight observation anchors")
    resolved = []
    for anchor in anchors:
        if (
            not isinstance(anchor, dict)
            or set(anchor) - {"turn_id", "observation_id"}
            or "observation_id" not in anchor
            or any(not isinstance(v, str) or not v for v in anchor.values())
        ):
            raise ValueError("An anchor requires an observation_id and optional turn_id")
        source_turn = anchor.get("turn_id", current_turn)
        if not source_turn:
            raise ValueError("An observation anchor requires its source turn")
        resolved.append({"turn_id": source_turn, "observation_id": anchor["observation_id"]})
    if len({(a["turn_id"], a["observation_id"]) for a in resolved}) != len(resolved):
        raise ValueError("Observation anchors must be unique")
    return copy.deepcopy({"work_note": text, "open_questions": questions, "anchors": resolved})


def pointer(value, path):
    """Read a literal JSON pointer; missing is an error, null remains null."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Use a JSON pointer beginning with /")
    node = value
    for part in path[1:].split("/"):
        if "~" in part.replace("~0", "").replace("~1", ""):
            raise ValueError("Invalid JSON pointer escape")
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif (
            isinstance(node, list)
            and key.isascii()
            and key.isdecimal()
            and (key == "0" or not key.startswith("0"))
            and int(key) < len(node)
        ):
            node = node[int(key)]
        else:
            raise ValueError("The requested source path is missing")
    return copy.deepcopy(node)
