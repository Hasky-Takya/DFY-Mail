"""Small, strict public configuration and extraction contract for graph memory."""

import copy
import json
import unicodedata


DEFAULTS = {
    "enabled": False, "max_updates": 16, "max_text_chars": 200,
    "context_items": 20, "context_chars": 12000, "query_limit": 30, "batch_size": 8,
    "assistant_name": "才羽桃井", "assistant_aliases": ["桃井", "助手"],
    "user_name": "老师", "user_aliases": ["用户"], "sqlite_cache_kib": 8192,
}
_LIMITS = {
    "max_updates": (1, 64), "max_text_chars": (1, 4000),
    "context_items": (1, 200), "context_chars": (256, 100000),
    "query_limit": (1, 200), "batch_size": (1, 64), "sqlite_cache_kib": (256, 262144),
}
KINDS = ("facts", "entities", "relations", "states", "event_relations")
ENTITY_TYPES = ("person", "group", "project", "place")
RELATION_PREDICATES = ("friend_of", "sibling_of", "planned_with", "member_of", "works_on", "participates_in", "organizes")
SYMMETRIC_PREDICATES = frozenset({"friend_of", "sibling_of", "planned_with"})
STATE_PREDICATES = ("working_on", "located_at", "busy_with")
EVENT_PREDICATES = ("follows", "part_of", "caused_by", "fulfills", "cancels")


def normalize_name(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def fixed_person_name(name, settings):
    """Map only explicitly configured names, never conversational pronouns."""
    normalized = normalize_name(name)
    if normalized in {"我", "你", "您", "我们", "你们"}:
        return name
    matches = [settings[role + "_name"] for role in ("assistant", "user")
               if normalized in {normalize_name(value) for value in
                                 [settings[role + "_name"], *settings[role + "_aliases"]]}]
    return matches[0] if len(matches) == 1 else name


# Explicit endpoint matrix. Events are canonical story events, not entities.
RELATION_TYPES = {
    "friend_of": ({"person"}, {"person"}),
    "sibling_of": ({"person"}, {"person"}),
    "planned_with": ({"person"}, {"person"}),
    "member_of": ({"person"}, {"group"}),
    "works_on": ({"person"}, {"project"}),
    "participates_in": ({"person"}, {"event", "project"}),
    "organizes": ({"person"}, {"event", "project"}),
}


def assertion_error(kind, item, endpoint_types):
    """Shared semantic check, separate from historical package structure/hash."""
    if kind not in ("relations", "event_relations"):
        return None
    if item["subject"] == item["object"]:
        return "self_loop"
    allowed = ({"event"}, {"event"}) if kind == "event_relations" else RELATION_TYPES[item["predicate"]]
    for field, types in zip(("subject", "object"), allowed):
        if endpoint_types.get(item[field]) not in types:
            return "invalid_endpoint_type"
    return None


def normalize_settings(value=None):
    """Return independent defaults. Null means defaults, never silently fix types."""
    if value is None:
        value = {}
    if not isinstance(value, dict) or value.keys() - DEFAULTS.keys():
        raise ValueError("Invalid graph settings section or unknown key.")
    result = copy.deepcopy(DEFAULTS)
    for key, supplied in value.items():
        if key == "enabled":
            if type(supplied) is not bool:
                raise ValueError("Graph enabled must be a boolean.")
        elif key.endswith("_name") or key.endswith("_aliases"):
            names = supplied if key.endswith("_aliases") else [supplied]
            if (not isinstance(names, list) or len(names) > 16 or
                    any(not isinstance(name, str) or not name.strip() or len(name) > 200
                        or any(char in name for char in "\n\r\v\f\x1c\x1d\x1e\x85  ") for name in names)):
                raise ValueError("Graph identity settings require short names or alias lists.")
            if len({normalize_name(name) for name in names}) != len(names):
                raise ValueError("Graph aliases must be distinct.")
        else:
            low, high = _LIMITS[key]
            if type(supplied) is not int or not low <= supplied <= high:
                raise ValueError("Graph numeric setting is outside its integer bounds.")
        result[key] = copy.deepcopy(supplied)
    return result


def graph_template(settings=None):
    """Return a prompt fragment, not executable instructions or a JSON Schema."""
    config = normalize_settings(settings)
    common = {"source": "user|llm", "domain": "shared_story|user_report|assistant_statement|user_preference|uncertain", "evidence": "exact nonempty quote from that current source body"}
    target = {"target_id": None, "expected_version": None}
    edge = {**target, "subject": "entity alias or displayed entity ID", "state": "active|retracted|contested", "support_ref": None, **common}
    example = {
        "facts": [{"ref": "displayed event/memory ID or E1/M1 from this story batch", "text": "short text view only", "category": "event|profile|preference|memory", **common}],
        "entities": [{"ref": "P1", "type": "person|group|project|place", "name": "canonical name", "aliases": [], **target, **common}],
        "relations": [{**edge, "predicate": "|".join(RELATION_PREDICATES), "object": "entity alias or displayed entity ID"}],
        "states": [{**edge, "predicate": "|".join(STATE_PREDICATES), "value": "short state value", "valid_until": None}],
        "event_relations": [{**edge, "subject": "displayed event ID or E1", "predicate": "|".join(EVENT_PREDICATES), "object": "displayed event ID or E1"}],
    }
    return (
        "Return exactly these five arrays (empty arrays are valid). At most "
        + str(config["max_updates"]) + " items TOTAL; text at most "
        + str(config["max_text_chars"]) + " characters, one line only (no line breaks). Never invent IDs or hidden targets. "
        "No self-loops. friend_of, sibling_of and planned_with are symmetric person-person relations. "
        "member_of is person-group; works_on is person-project; participates_in and organizes are "
        "person-event or person-project (never person-person). Event relations are event-event. "
        "For edits supply a displayed target_id and its exact expected_version; keep identity/endpoints unchanged. "
        "New items use null target_id/expected_version. Evidence must be verbatim current text; "
        "user evidence is unavailable for proactive messages. No image-only evidence is accepted. "
        "Facts are views, never story status updates. Fixed person identities (same domain only, never map 我/你): "
        + json.dumps({role: {"name": config[role + "_name"], "aliases": config[role + "_aliases"]}
                      for role in ("assistant", "user")}, ensure_ascii=False, sort_keys=True)
        + ". Candidate example/schema:\n"
        + json.dumps(example, ensure_ascii=False, sort_keys=True)
    )
