"""Pure graph extraction validation and immutable, content-addressed replay packages.

No I/O, model calls, clock, or story mutations. Only displayed context can be
referenced; caller-owned local story aliases are resolved when freezing.
"""

import copy
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime

from graph_schema import (KINDS, ENTITY_TYPES, RELATION_PREDICATES, SYMMETRIC_PREDICATES,
                          STATE_PREDICATES, EVENT_PREDICATES, normalize_settings,
                          assertion_error, fixed_person_name)
from story_memory import DOMAINS as _DOMAINS

_CODES = {
    "invalid_fields", "invalid_type", "invalid_enum", "invalid_time", "invalid_settings",
    "unknown_target", "duplicate_target", "version_conflict", "immutable_identity",
    "unknown_reference", "duplicate_identity", "source_domain_mismatch",
    "evidence_not_in_source", "invalid_binding", "invalid_package", "too_many_updates",
    "self_loop", "invalid_endpoint_type",
}
_FIELDS = {"ref", "type", "name", "aliases", "target_id", "expected_version", "source", "domain",
           "evidence", "text", "category", "subject", "predicate", "object", "value", "state",
           "valid_until", "support_ref", "source_refs", "id", "version", "observed_at"}
_COMMON = {"source", "domain", "evidence"}
_TARGET = {"target_id", "expected_version"}
_CANDIDATE_FIELDS = {
    "entities": _COMMON | _TARGET | {"ref", "type", "name", "aliases"},
    "facts": _COMMON | {"ref", "text", "category"},
    "relations": _COMMON | _TARGET | {"subject", "predicate", "object", "state", "support_ref"},
    "states": _COMMON | _TARGET | {"subject", "predicate", "value", "state", "valid_until", "support_ref"},
    "event_relations": _COMMON | _TARGET | {"subject", "predicate", "object", "state", "support_ref"},
}
_SOURCE_KINDS = {"events": "event", "memories": "memory", "entities": "entity", "facts": "fact",
                 "relations": "relation", "states": "state", "event_relations": "event_relation"}


def _safe_path(path):
    if not isinstance(path, str):
        return "$"
    if path in {"$", *KINDS, *_FIELDS}:
        return path
    if isinstance(path, str):
        m = re.fullmatch(r"(facts|entities|relations|states|event_relations)\[([0-9]{1,2})\](?:\.([a-z_]+))?", path)
        if m and int(m[2]) < 64 and (m[3] is None or m[3] in _FIELDS):
            return path
    return "$"


class GraphError(ValueError):
    """Controlled error: neither exception text nor safe_details contain input."""

    def __init__(self, message="Invalid graph data.", *, code="invalid_fields", path="$"):
        self.code = code if isinstance(code, str) and code in _CODES else "invalid_fields"
        self.path = _safe_path(path)
        super().__init__("Graph validation failed: " + self.code + ".")

    def safe_details(self):
        code = self.code if isinstance(self.code, str) and self.code in _CODES else "invalid_fields"
        return {"code": code, "path": _safe_path(self.path), "hint": "Return only the documented graph schema and displayed references."}


def _fail(code="invalid_fields", path="$"):
    error = GraphError(code=code, path=path)
    logging.getLogger("dfy.graph").warning("graph_validation_rejected code=%s path=%s", error.code, error.path)
    raise error


def digest(value):
    """Canonical JSON SHA-256; bool/int and list order remain distinct."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail("invalid_type")


def stable_id(kind, *parts):
    _text(kind, "id", 40)
    return kind + "_" + digest([kind, *parts])


def empty_candidate():
    return {kind: [] for kind in KINDS}


def _text(value, path, limit=200, nullable=False):
    if value is None and nullable:
        return
    if not isinstance(value, str):
        _fail("invalid_type", path)
    if not value.strip() or len(value) > limit:
        _fail("invalid_fields", path)


def _short_text(value, path, limit):
    _text(value, path, limit)
    # Short graph views are one logical line; evidence remains verbatim and may
    # span lines. Reject Unicode line separators as well as CR/LF.
    if any(char in value for char in "\n\r\v\f\x1c\x1d\x1e\x85  "):
        _fail("invalid_fields", path)


def _int(value, path, minimum=1):
    if type(value) is not int or value < minimum:
        _fail("invalid_type", path)


def _enum(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        _fail("invalid_enum", path)


def _time(value, path):
    _text(value, path, 100)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})", value):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        _fail("invalid_time", path)


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _source_ref(kind, item):
    return {"kind": kind, "id": item["id"], "version": item["version"], "digest": digest(item)}


def _index(context, kinds):
    if not isinstance(context, dict):
        _fail("invalid_type")
    result = {}
    for kind in kinds:
        values = context.get(kind, [])
        if not isinstance(values, list):
            _fail("invalid_type", kind)
        for item in values:
            if not isinstance(item, dict):
                _fail("invalid_type", kind)
            _text(item.get("id"), "id", 200)
            _int(item.get("version"), "version")
            if item["id"] in result:
                _fail("duplicate_identity")
            result[item["id"]] = (kind, copy.deepcopy(item))
    return result


def _parse(raw):
    if not isinstance(raw, str):
        return raw
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                _fail("invalid_fields")
            result[key] = value
        return result
    def constant(value):
        _fail("invalid_type")
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        _fail("invalid_fields")


def _fields(item, kind, path, limit, stored=False):
    if not isinstance(item, dict):
        _fail("invalid_type", path)
    expected = _CANDIDATE_FIELDS[kind]
    if stored:
        expected = (expected - _TARGET - ({"ref"} if kind == "entities" else set())) | {"id", "version"}
    if set(item) != expected:
        _fail("invalid_fields", path)
    _enum(item["source"], {"user", "llm"}, path + ".source")
    _enum(item["domain"], _DOMAINS, path + ".domain")
    required = {"user_report": "user", "user_preference": "user", "assistant_statement": "llm"}.get(item["domain"])
    if required is not None and required != item["source"]:
        _fail("source_domain_mismatch", path + ".domain")
    if not stored:
        _text(item["evidence"], path + ".evidence", 1000)
        if kind != "facts":
            _text(item["target_id"], path + ".target_id", nullable=True)
            if item["target_id"] is None:
                if item["expected_version"] is not None:
                    _fail("version_conflict", path + ".expected_version")
            else:
                _int(item["expected_version"], path + ".expected_version")
    else:
        _text(item["id"], path + ".id")
        _int(item["version"], path + ".version")
    if kind == "entities":
        if not stored:
            _text(item["ref"], path + ".ref", 32)
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", item["ref"]) is None:
                _fail("invalid_fields", path + ".ref")
        _enum(item["type"], ENTITY_TYPES, path + ".type")
        _short_text(item["name"], path + ".name", limit)
        aliases = item["aliases"]
        if not isinstance(aliases, list) or len(aliases) > 16:
            _fail("invalid_fields", path + ".aliases")
        for alias in aliases:
            _short_text(alias, path + ".aliases", limit)
        if len({_norm(alias) for alias in aliases}) != len(aliases):
            _fail("invalid_fields", path + ".aliases")
    elif kind == "facts":
        _text(item["ref"], path + ".ref")
        _short_text(item["text"], path + ".text", limit)
        _enum(item["category"], {"event", "profile", "preference", "memory"}, path + ".category")
    else:
        _text(item["subject"], path + ".subject")
        predicates = {"relations": RELATION_PREDICATES, "states": STATE_PREDICATES, "event_relations": EVENT_PREDICATES}[kind]
        _enum(item["predicate"], predicates, path + ".predicate")
        _enum(item["state"], {"active", "retracted", "contested"}, path + ".state")
        _text(item["support_ref"], path + ".support_ref", nullable=True)
        if kind == "states":
            _short_text(item["value"], path + ".value", limit)
            if item["valid_until"] is not None:
                _time(item["valid_until"], path + ".valid_until")
        else:
            _text(item["object"], path + ".object")
            if stored and kind == "relations" and item["predicate"] in SYMMETRIC_PREDICATES and item["subject"] > item["object"]:
                _fail("invalid_binding", path)


def _canonical_endpoints(kind, item):
    if kind == "relations" and item["predicate"] in SYMMETRIC_PREDICATES:
        item["subject"], item["object"] = sorted((item["subject"], item["object"]))


def _identity(kind, item):
    if kind == "entities":
        return [item["domain"], item["type"], _norm(item["name"])]
    if kind == "facts":
        return [item["domain"], item["ref"], item["category"]]
    subject = item["subject"]
    object_id = item.get("object")
    if kind == "relations" and item["predicate"] in SYMMETRIC_PREDICATES:
        subject, object_id = sorted((subject, object_id))
    result = [item["domain"], subject, item["predicate"]]
    if kind != "states":
        result.append(object_id)
    return result


def validate_candidate(raw, *, story_context, graph_context=None, user_body, assistant_body,
                       settings=None, source_evidence=None):
    """Return five normalized arrays plus private _bindings; do not persist it.

    Initial conservative policy: only quotes from current text bodies are accepted.
    Caller MUST pass original user text (user_evidence.text), not a normalized
    body containing image observations. source_evidence never broadens authority;
    image-only evidence is unsupported. E1/M1 are unresolved story-batch aliases,
    checked during freezing.
    """
    try:
        config = normalize_settings(settings)
    except ValueError:
        _fail("invalid_settings")
    raw = _parse(raw)
    if not isinstance(raw, dict) or set(raw) != set(KINDS):
        _fail("invalid_fields")
    if (user_body is not None and not isinstance(user_body, str)) or not isinstance(assistant_body, str):
        _fail("invalid_type")
    story = _index(story_context, ("events", "memories"))
    graph = _index({} if graph_context is None else graph_context, KINDS)
    if set(story) & set(graph):
        _fail("duplicate_identity")
    for kind in KINDS:
        if not isinstance(raw[kind], list):
            _fail("invalid_type", kind)
    if sum(len(raw[kind]) for kind in KINDS) > config["max_updates"]:
        _fail("too_many_updates")
    result = copy.deepcopy(raw)
    used = {}
    aliases = {}
    targets = set()
    identities = set()

    def use(identifier, kinds, path, item=None):
        if isinstance(identifier, str) and re.fullmatch(r"[EM][1-9][0-9]*", identifier):
            if (identifier[0] == "E" and "events" in kinds) or (identifier[0] == "M" and "memories" in kinds):
                return identifier
        entry = story.get(identifier) or graph.get(identifier)
        if entry is None or entry[0] not in kinds:
            _fail("unknown_reference", path)
        kind, source_item = entry
        if item is not None and (source_item.get("domain") != item["domain"] or source_item.get("source") != item["source"]):
            _fail("source_domain_mismatch", path)
        used[identifier] = _source_ref(_SOURCE_KINDS[kind], source_item)
        return identifier

    # Establish aliases first: entities can appear after the edges in JSON.
    for i, item in enumerate(result["entities"]):
        path = f"entities[{i}]"
        _fields(item, "entities", path, config["max_text_chars"])
        alias = item["ref"]
        if alias in aliases or alias in story or alias in graph or re.fullmatch(r"[EM][1-9][0-9]*", alias):
            _fail("duplicate_identity", path + ".ref")
        target = item["target_id"]
        if target is None and item["type"] == "person":
            original_name = item["name"]
            item["name"] = fixed_person_name(original_name, config)
            if item["name"] != original_name and original_name not in item["aliases"]:
                item["aliases"].append(original_name)
        aliases[alias] = target or stable_id("entity", *_identity("entities", item))

    endpoint_types = {identifier: "event" for identifier, (kind, _) in story.items() if kind == "events"}
    endpoint_types.update({identifier: item.get("type") for identifier, (kind, item) in graph.items() if kind == "entities"})
    endpoint_types.update({aliases[item["ref"]]: item["type"] for item in result["entities"]})

    for kind in KINDS:
        for i, item in enumerate(result[kind]):
            path = f"{kind}[{i}]"
            _fields(item, kind, path, config["max_text_chars"])
            body = user_body if item["source"] == "user" else assistant_body
            if body is None or item["evidence"] not in body:
                _fail("evidence_not_in_source", path + ".evidence")
            if kind == "facts":
                use(item["ref"], {"events", "memories"}, path + ".ref", item)
                match = re.fullmatch(r"([EM])[1-9][0-9]*", item["ref"])
                ref_kind = ("events" if match[1] == "E" else "memories") if match else story.get(item["ref"], (None,))[0]
                if (item["category"] == "event") != (ref_kind == "events"):
                    _fail("unknown_reference", path + ".ref")
            elif kind != "entities":
                for endpoint in (("subject",) if kind == "states" else ("subject", "object")):
                    value = item[endpoint]
                    if kind != "event_relations" and value in aliases:
                        item[endpoint] = aliases[value]
                    else:
                        allowed = {"events"} if kind == "event_relations" else {"entities"}
                        if kind == "relations" and endpoint == "object" and item["predicate"] in {"participates_in", "organizes"}:
                            allowed = {"entities", "events"}
                        use(value, allowed, path + "." + endpoint)
                        if re.fullmatch(r"E[1-9][0-9]*", value):
                            endpoint_types[value] = "event"
                if item["support_ref"] is not None:
                    use(item["support_ref"], {"events", "memories"}, path + ".support_ref", item)
                _canonical_endpoints(kind, item)
            target = item.get("target_id")
            if target is not None:
                entry = graph.get(target)
                if entry is None or entry[0] != kind:
                    _fail("unknown_target", path + ".target_id")
                old = entry[1]
                if target in targets:
                    _fail("duplicate_target", path + ".target_id")
                targets.add(target)
                if old["version"] != item["expected_version"]:
                    _fail("version_conflict", path + ".expected_version")
                if _identity(kind, old) != _identity(kind, item):
                    _fail("immutable_identity", path)
                used[target] = _source_ref(_SOURCE_KINDS[kind], old)
            semantic = digest([kind, _identity(kind, item)])
            if semantic in identities:
                _fail("duplicate_identity", path)
            identities.add(semantic)
            if target is None and kind != "facts":
                for old_kind, old in graph.values():
                    same = old_kind == kind and _identity(kind, old) == _identity(kind, item)
                    if old_kind == kind == "entities" and old["type"] == item["type"] == "person":
                        same = (old["domain"] == item["domain"] and
                                _norm(fixed_person_name(old["name"], config)) == _norm(fixed_person_name(item["name"], config)))
                    if same:
                        _fail("unknown_target", path + ".target_id")
    entity_identities = {identifier: (item["domain"], item["type"],
                         _norm(fixed_person_name(item["name"], config) if item["type"] == "person" else item["name"]))
                         for identifier, (kind, item) in graph.items() if kind == "entities"}
    entity_identities.update({aliases[item["ref"]]: (item["domain"], item["type"],
                              _norm(fixed_person_name(item["name"], config) if item["type"] == "person" else item["name"]))
                              for item in result["entities"]})
    for kind in ("relations", "event_relations"):
        for i, item in enumerate(result[kind]):
            code = assertion_error(kind, item, endpoint_types)
            if (kind == "relations" and item["subject"] in entity_identities
                    and entity_identities[item["subject"]] == entity_identities.get(item["object"])):
                code = "self_loop"
            if code:
                _fail(code, f"{kind}[{i}]")
    result["_bindings"] = {
        "candidate_digest": digest(result), "sources": list(used.values()),
        "story": {key: item for key, (_, item) in story.items()},
        "graph": {key: item for key, (_, item) in graph.items()},
        "mail": {"user": digest(user_body) if user_body is not None else None, "llm": digest(assistant_body)},
    }
    return result


def _seal(package):
    package["package_id"] = stable_id("package", {key: value for key, value in package.items() if key != "package_id"})
    return package


def incomplete_package(dialog_id, observed_at, reason):
    _text(dialog_id, "id")
    _time(observed_at, "observed_at")
    # Keep diagnostics controlled: arbitrary exception strings must not persist.
    _enum(reason, _CODES | {"extraction_failed", "disabled", "story_incomplete", "unavailable", "retry_exhausted"}, "$")
    return _seal({"schema_version": 1, "dialog_id": dialog_id, "observed_at": observed_at,
                  "status": "incomplete", "reason": reason, "source_refs": [], **empty_candidate()})


def freeze_candidate(validated, *, dialog_id, story_package=None, observed_at,
                     user_message_id, assistant_message_id, story_context=None):
    """Resolve E1/M1, freeze provenance and deterministic absolute item versions."""
    _text(dialog_id, "id")
    _time(observed_at, "observed_at")
    _text(user_message_id, "id", 1000, nullable=True)
    _text(assistant_message_id, "id", 1000)
    if not isinstance(validated, dict) or set(validated) != {*KINDS, "_bindings"}:
        _fail("invalid_binding")
    bindings = validated["_bindings"]
    candidate = {kind: copy.deepcopy(validated[kind]) for kind in KINDS}
    if not isinstance(bindings, dict) or bindings.get("candidate_digest") != digest(candidate):
        _fail("invalid_binding")
    story = bindings["story"]
    if story_context is not None:
        current = _index(story_context, ("events", "memories"))
        for identifier, old in story.items():
            if identifier not in current or digest(current[identifier][1]) != digest(old):
                _fail("invalid_binding")
    local = {}
    if story_package is not None:
        from story_memory import validate_package as validate_story_package
        try:
            validate_story_package(story_package)
        except ValueError:
            _fail("invalid_binding")
        if story_package["dialog_id"] != dialog_id or story_package["observed_at"] != observed_at:
            _fail("invalid_binding")
        for prefix, key, kind in (("E", "event_updates", "event"), ("M", "memory_updates", "memory")):
            for i, item in enumerate(story_package[key], 1):
                local[prefix + str(i)] = (kind, item)
    # The story projector applies this turn before the graph projector. Rebind
    # displayed old IDs updated by this story package to that committed version.
    current_story = {item["id"]: (kind, item) for kind, item in local.values()}
    sources = {(ref["kind"], ref["id"]): copy.deepcopy(ref) for ref in bindings["sources"]}
    for key in list(sources):
        if key[0] in {"event", "memory"} and key[1] in current_story:
            kind, source = current_story[key[1]]
            if kind != key[0]:
                _fail("invalid_binding")
            sources[key] = _source_ref(kind, source)

    def resolve(value, item=None):
        if value in current_story:
            _, source = current_story[value]
            if item is not None and (source.get("source") != item["source"] or source.get("domain") != item["domain"]):
                _fail("source_domain_mismatch")
        if value in local:
            kind, source = local[value]
            if item is not None and (source.get("source") != item["source"] or source.get("domain") != item["domain"]):
                _fail("source_domain_mismatch")
            ref = _source_ref(kind, source)
            sources[(kind, source["id"])] = ref
            return source["id"]
        if isinstance(value, str) and re.fullmatch(r"[EM][1-9][0-9]*", value):
            _fail("unknown_reference")
        return value

    package = {"schema_version": 1, "dialog_id": dialog_id, "observed_at": observed_at,
               "status": "complete", "reason": None, "source_refs": [], **empty_candidate()}
    seen = set()
    for kind in KINDS:
        for update in candidate[kind]:
            item = {key: value for key, value in update.items() if key not in _TARGET and not (kind == "entities" and key == "ref")}
            if kind == "facts":
                item["ref"] = resolve(item["ref"], item)
            elif kind != "entities":
                if kind in {"relations", "event_relations"}:
                    item["subject"] = resolve(item["subject"])
                    item["object"] = resolve(item["object"])
                item["support_ref"] = resolve(item["support_ref"], item)
                _canonical_endpoints(kind, item)
            target = update.get("target_id")
            identifier = target or stable_id(_SOURCE_KINDS[kind], *_identity(kind, item))
            previous = bindings["graph"].get(identifier)
            if kind == "facts" and previous is not None:
                sources[("fact", identifier)] = _source_ref("fact", previous)
            item["id"] = identifier
            item["version"] = previous["version"] + 1 if previous else 1
            message_id = user_message_id if item["source"] == "user" else assistant_message_id
            if message_id is None or bindings["mail"].get(item["source"]) is None:
                _fail("invalid_binding")
            item["evidence"] = {"source": item["source"], "quote": update["evidence"],
                                "message_id": message_id, "dialog_id": dialog_id}
            mail = {"kind": "mail", "id": message_id, "version": 1, "digest": bindings["mail"][item["source"]]}
            if ("mail", message_id) in sources and sources[("mail", message_id)] != mail:
                _fail("invalid_binding")
            sources[("mail", message_id)] = mail
            if identifier in seen:
                _fail("duplicate_identity")
            seen.add(identifier)
            package[kind].append(item)
    endpoint_types = {ref["id"]: "event" for ref in sources.values() if ref["kind"] == "event"}
    endpoint_types.update({item["id"]: "event" for kind, item in local.values() if kind == "event"})
    endpoint_types.update({identifier: item.get("type") for identifier, item in bindings["graph"].items() if "type" in item})
    endpoint_types.update({item["id"]: item["type"] for item in package["entities"]})
    for kind in ("relations", "event_relations"):
        for i, item in enumerate(package[kind]):
            code = assertion_error(kind, item, endpoint_types)
            if code:
                _fail(code, f"{kind}[{i}]")
    package["source_refs"] = sorted(sources.values(), key=lambda ref: (ref["kind"], ref["id"]))
    _seal(package)
    return validate_package(package)


def validate_package(package, record=None):
    """Pure structural/content-hash checks; optional record binds dialog and mail IDs.

    No raw bodies are required for replay. Canonical external source digests are
    checked by the projector, which has the authoritative story/source index.
    """
    fields = {"schema_version", "package_id", "dialog_id", "observed_at", "status", "reason", "source_refs", *KINDS}
    if not isinstance(package, dict) or set(package) != fields:
        _fail("invalid_package")
    if type(package["schema_version"]) is not int or package["schema_version"] != 1:
        _fail("invalid_package")
    _text(package["dialog_id"], "id")
    _time(package["observed_at"], "observed_at")
    _enum(package["status"], {"complete", "incomplete"}, "$")
    if package["status"] == "complete":
        if package["reason"] is not None:
            _fail("invalid_package")
    else:
        _enum(package["reason"], _CODES | {"extraction_failed", "disabled", "story_incomplete", "unavailable", "retry_exhausted"}, "$")
    expected_id = stable_id("package", {key: value for key, value in package.items() if key != "package_id"})
    if package["package_id"] != expected_id:
        _fail("invalid_binding")
    refs = package["source_refs"]
    if not isinstance(refs, list) or len(refs) > 1024:
        _fail("invalid_fields", "source_refs")
    sources = {}
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"kind", "id", "version", "digest"}:
            _fail("invalid_fields", "source_refs")
        _enum(ref["kind"], {*_SOURCE_KINDS.values(), "mail"}, "source_refs")
        _text(ref["id"], "source_refs", 1000)
        _int(ref["version"], "source_refs")
        if ref["kind"] == "mail" and ref["version"] != 1:
            _fail("invalid_binding", "source_refs")
        if not isinstance(ref["digest"], str) or re.fullmatch(r"[0-9a-f]{64}", ref["digest"]) is None:
            _fail("invalid_binding", "source_refs")
        key = (ref["kind"], ref["id"])
        if key in sources:
            _fail("duplicate_identity", "source_refs")
        sources[key] = ref
    if record is not None:
        if not isinstance(record, dict) or record.get("id") != package["dialog_id"]:
            _fail("invalid_binding")
        message_ids = {record.get("user_message_id"), record.get("assistant_message_id")}
        for kind, identifier in sources:
            if kind == "mail" and identifier not in message_ids:
                _fail("invalid_binding", "source_refs")
        story_package = record.get("story_updates")
        if story_package is not None:
            from story_memory import validate_package as validate_story_package
            try:
                validate_story_package(story_package)
            except ValueError:
                _fail("invalid_binding")
            if story_package["dialog_id"] != package["dialog_id"]:
                _fail("invalid_binding")
            for kind, key in (("event", "event_updates"), ("memory", "memory_updates")):
                for source in story_package[key]:
                    bound = sources.get((kind, source["id"]))
                    if bound is not None and bound != _source_ref(kind, source):
                        _fail("invalid_binding", "source_refs")
    seen = {}
    total = 0
    for kind in KINDS:
        items = package[kind]
        if not isinstance(items, list):
            _fail("invalid_type", kind)
        total += len(items)
        for i, item in enumerate(items):
            path = f"{kind}[{i}]"
            _fields(item, kind, path, 4000, stored=True)
            if item["id"] in seen:
                _fail("duplicate_identity", path)
            seen[item["id"]] = kind
            if item["id"] != stable_id(_SOURCE_KINDS[kind], *_identity(kind, item)) and item["version"] == 1:
                _fail("invalid_binding", path)
            evidence = item["evidence"]
            if not isinstance(evidence, dict) or set(evidence) != {"source", "quote", "message_id", "dialog_id"}:
                _fail("invalid_fields", path + ".evidence")
            _text(evidence["quote"], path + ".evidence", 1000)
            _text(evidence["message_id"], path + ".evidence", 1000)
            if evidence["source"] != item["source"] or evidence["dialog_id"] != package["dialog_id"]:
                _fail("invalid_binding", path + ".evidence")
            if ("mail", evidence["message_id"]) not in sources:
                _fail("invalid_binding", path + ".evidence")
            if record is not None:
                key = "user_message_id" if item["source"] == "user" else "assistant_message_id"
                if evidence["message_id"] != record.get(key):
                    _fail("invalid_binding", path + ".evidence")
            if item["version"] > 1:
                prior = sources.get((_SOURCE_KINDS[kind], item["id"]))
                if prior is None or prior["version"] + 1 != item["version"]:
                    _fail("version_conflict", path)
    if total > 64 or (package["status"] == "incomplete" and (total or refs)):
        _fail("invalid_package")
    for kind in KINDS:
        for item in package[kind]:
            checks = []
            if kind == "facts":
                checks.append((item["ref"], {"event"} if item["category"] == "event" else {"memory"}))
            elif kind != "entities":
                endpoint_kind = {"event"} if kind == "event_relations" else {"entity"}
                checks.append((item["subject"], endpoint_kind))
                if kind != "states":
                    object_kinds = endpoint_kind
                    if kind == "relations" and item["predicate"] in {"participates_in", "organizes"}:
                        object_kinds = {"entity", "event"}
                    checks.append((item["object"], object_kinds))
                if item["support_ref"] is not None:
                    checks.append((item["support_ref"], {"event", "memory"}))
            for identifier, allowed in checks:
                local_kind = _SOURCE_KINDS.get(seen.get(identifier))
                if local_kind not in allowed and not any((source_kind, identifier) in sources for source_kind in allowed):
                    _fail("unknown_reference")
    return package
