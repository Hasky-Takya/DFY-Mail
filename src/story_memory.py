"""Deterministic story projections. Call reconcile_locked only under the dialog lock.

No model calls, dialog imports, wall clock, or dialog writes live in this module.
The sent dialog records are the authority; the two JSON files are disposable,
verified prefixes of that authority, never an independent source of truth.
"""

import copy
import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path


class StoryError(RuntimeError):
    """Persistence, corruption, or replay conflict; stop the caller."""


# Only these controlled descriptions may be logged or sent back to the model.
_VALIDATION_HINTS = {
    "invalid_update": "Return an update matching the required JSON schema.",
    "evidence_not_in_source": "Copy evidence exactly from the selected current source body.",
    "raw_time_not_in_source": "Copy raw event time exactly from the selected current source body.",
    "unknown_target": "Use null for a new item or an existing same-kind target ID.",
    "duplicate_target": "Update each existing target at most once per batch.",
    "immutable_origin": "Keep the existing memory origin unchanged.",
    "invalid_fields": "Use exactly the required fields and respect field size and uniqueness limits.",
    "invalid_enum": "Choose one of the allowed schema values for this field.",
    "invalid_type": "Use the JSON type required by the schema for this field.",
    "invalid_time": "Use null or a valid timezone-aware ISO 8601 time; keep end at or after start and retain raw evidence.",
    "invalid_importance": "Use an integer importance from 1 through 5, not a boolean.",
    "unknown_related_event": "Reference only event IDs already present in story context.",
    "source_domain_mismatch": "Use user evidence for user_report/user_preference and llm evidence for assistant_statement.",
    "image_requires_uncertain": "Use the uncertain domain for evidence taken from an image observation.",
}
_SAFE_FIELD_PATHS = {
    "target_id", "evidence", "content", "source", "domain", "importance", "people", "status",
    "title", "tense", "event_time", "event_time.raw", "event_time.start", "event_time.end",
    "event_time.anchor", "category", "origin", "related_event_ids", "id", "version",
    "created_at", "updated_at", "source_refs", "revision", "applied_dialog_ids",
    "dialog_id", "user_message_id", "assistant_message_id", "received_at_source",
    "anchors", "anchors.as_of", "anchors.received_at", "anchors.received_at_source",
    "user_evidence", "user_evidence.text", "user_evidence.images",
    "evidence_origin", "image_ids",
}
_FIELD_NAMES = {
    "event status": "status", "memory status": "status", "raw event time": "event_time.raw",
    "source dialog ID": "source_refs", "reference source": "source_refs",
    "source message ID": "source_refs", "quote": "source_refs",
}


def _safe_path(path):
    # Never accept model keys, arbitrary dotted paths, or text masquerading as an index.
    if not isinstance(path, str):
        return "$"
    if path == "$" or path in _SAFE_FIELD_PATHS or path in {"event_updates", "memory_updates"}:
        return path
    match = re.fullmatch(r"(event_updates|memory_updates)\[([0-9]{1,2})\](?:\.(.+))?", path)
    if match and int(match[2]) < 32 and (match[3] is None or match[3] in _SAFE_FIELD_PATHS):
        return path
    return "$"


class StoryValidationError(ValueError):
    """Untrusted extraction failed; safe_details excludes exception text and values."""

    def __init__(self, message, *, code="invalid_update", path="$"):
        super().__init__(message)
        self.code = code if isinstance(code, str) and code in _VALIDATION_HINTS else "invalid_update"
        self.path = _safe_path(path)

    def safe_details(self):
        # Re-check public attributes so even accidental reassignment cannot leak data.
        code = self.code if isinstance(self.code, str) and self.code in _VALIDATION_HINTS else "invalid_update"
        return {"code": code, "path": _safe_path(self.path), "hint": _VALIDATION_HINTS[code]}


MAX_UPDATES = 32
MAX_TEXT = 4000
MAX_REFS = 10000
KINDS = ("events", "memories")
DOMAINS = {"shared_story", "user_report", "assistant_statement", "user_preference", "uncertain"}
COMMON = {"content", "source", "domain", "importance", "people", "status"}
EVENT = {"title", "tense", "event_time"}
MEMORY = {"category", "origin", "related_event_ids"}
GENERATED = {"id", "version", "created_at", "updated_at", "origin", "source_refs"}


def empty_context() -> dict:
    return {"revision": 0, "applied_dialog_ids": [], "events": [], "memories": []}


def _fail(message, *, code="invalid_update", path="$"):
    raise StoryValidationError(message, code=code, path=path)


def _field_path(name):
    # name is a developer-supplied schema label, never an input dictionary key.
    return _safe_path(_FIELD_NAMES.get(name, name))


def _update_fields(item, kind, prefix, *, check_domain=True):
    try:
        _fields(item, kind, check_domain=check_domain)
    except StoryValidationError as exc:
        exc.path = _safe_path(prefix if exc.path == "$" else f"{prefix}.{exc.path}")
        raise


def _json_loads(text):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                _fail("Duplicate JSON object key.")
            result[key] = value
        return result

    def constant(value):
        _fail("Non-finite JSON number.")

    return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)


def _same(left, right):
    # Python considers True == 1 == 1.0; stored schema types must not drift.
    return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)


def _string(value, name, limit=200, *, nullable=False, empty=False):
    if value is None and nullable:
        return
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        _fail(f"Invalid {name}.", code="invalid_type" if not isinstance(value, str) else "invalid_fields",
              path=_field_path(name))


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        _fail(f"Invalid {name}.", code="invalid_importance" if name == "importance" else "invalid_type",
              path=_field_path(name))


def _time(value, name="timezone-aware ISO timestamp"):
    path = _field_path(name)
    try:
        _string(value, "timezone-aware ISO timestamp", 100)
    except StoryValidationError as exc:
        exc.code = "invalid_time"
        exc.path = path
        raise
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", value):
        _fail("Timestamp must be timezone-aware ISO 8601.", code="invalid_time", path=path)
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        _fail("Timestamp must be timezone-aware ISO 8601.", code="invalid_time", path=path)


def _enum(value, choices, name):
    if not isinstance(value, str) or value not in choices:
        _fail(f"Invalid {name}.", code="invalid_enum" if isinstance(value, str) else "invalid_type",
              path=_field_path(name))


def _strings(values, name, limit=32):
    if not isinstance(values, list) or len(values) > limit:
        _fail(f"Invalid {name} list.", code="invalid_fields" if isinstance(values, list) else "invalid_type",
              path=_field_path(name))
    for value in values:
        _string(value, name)
    if len(values) != len(set(values)):
        _fail(f"Duplicate {name}.", code="invalid_fields", path=_field_path(name))


def _image_source(value, source):
    """Only the single-image provenance shape generated by this module is valid."""
    if not isinstance(value, dict) or set(value) != {"evidence_origin", "image_ids"}:
        _fail("Invalid image source fields.", code="invalid_fields", path="source_refs")
    if value["evidence_origin"] != "image_observation" or source != "user":
        _fail("Image observations require a user source.", code="invalid_fields", path="evidence_origin")
    ids = value["image_ids"]
    if (not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str)
            or re.fullmatch(r"image-[1-4]", ids[0]) is None):
        _fail("Invalid image IDs.", code="invalid_fields", path="image_ids")


def _user_evidence(value):
    """Validate trusted caller metadata without permitting a concatenated source."""
    if not isinstance(value, dict) or set(value) != {"text", "images"}:
        _fail("Invalid program user evidence.", code="invalid_fields", path="user_evidence")
    if not isinstance(value["text"], str):
        _fail("Program user text must be text.", code="invalid_type", path="user_evidence.text")
    images = value["images"]
    if not isinstance(images, list) or len(images) > 4:
        _fail("Invalid program image evidence.", code="invalid_fields", path="user_evidence.images")
    seen = set()
    for image in images:
        if (not isinstance(image, dict) or set(image) != {"image_id", "text"}
                or not isinstance(image["image_id"], str)
                or re.fullmatch(r"image-[1-4]", image["image_id"]) is None
                or not isinstance(image["text"], str) or image["image_id"] in seen):
            _fail("Invalid program image evidence.", code="invalid_fields", path="user_evidence.images")
        seen.add(image["image_id"])


def _fields(item, kind, *, stored=False, check_domain=True):
    if not isinstance(item, dict):
        _fail("Update must be an object.", code="invalid_type")
    fields = COMMON | (EVENT if kind == "events" else MEMORY)
    required = fields | (GENERATED if stored else {"target_id", "evidence"})
    if not stored and kind == "memories":
        required = required - {"related_event_ids"}
    if not required <= item.keys() or item.keys() - (required | {"related_event_ids"} if kind == "memories" else required):
        _fail("Unknown or missing update fields.", code="invalid_fields")
    _string(item["content"], "content", MAX_TEXT)
    _enum(item["source"], {"user", "llm"}, "source")
    _enum(item["domain"], DOMAINS, "domain")
    required_source = {"user_report": "user", "user_preference": "user",
                       "assistant_statement": "llm"}.get(item["domain"])
    if check_domain and required_source is not None and item["source"] != required_source:
        _fail("Domain does not match its evidence source.", code="source_domain_mismatch", path="domain")
    _integer(item["importance"], "importance", 1)
    if item["importance"] > 5:
        _fail("Importance must be between 1 and 5.", code="invalid_importance", path="importance")
    _strings(item["people"], "people")
    if kind == "events":
        _string(item["title"], "title")
        _enum(item["status"], {"planned", "ongoing", "completed"}, "event status")
        _enum(item["tense"], {"past", "present", "future", "unknown"}, "tense")
        event_time = item["event_time"]
        expected_time = {"raw", "start", "end", "anchor"} if stored else {"raw", "start", "end"}
        if not isinstance(event_time, dict) or set(event_time) != expected_time:
            _fail("Invalid event_time.", code="invalid_fields" if isinstance(event_time, dict) else "invalid_type",
                  path="event_time")
        if stored:
            _time(event_time["anchor"], "event_time.anchor")
        _string(event_time["raw"], "raw event time", 200, nullable=True)
        for field in ("start", "end"):
            if event_time[field] is not None:
                _time(event_time[field], f"event_time.{field}")
        if event_time["start"] is not None and event_time["end"] is not None:
            if _time(event_time["end"], "event_time.end") < _time(event_time["start"], "event_time.start"):
                _fail("Event end precedes start.", code="invalid_time", path="event_time.end")
        if event_time["raw"] is None and any(event_time[k] is not None for k in ("start", "end")):
            _fail("Normalized event time needs raw evidence.", code="invalid_time", path="event_time.raw")
    else:
        _enum(item["status"], {"active", "superseded", "retracted"}, "memory status")
        _string(item["category"], "category")
        _string(item["origin"], "origin")
        _strings(item.get("related_event_ids", []), "related_event_ids", MAX_UPDATES)
    if not stored:
        _string(item["target_id"], "target_id", nullable=True)
        _string(item["evidence"], "evidence", 1000)
        return
    _string(item["id"], "id")
    _integer(item["version"], "version", 1)
    _time(item["created_at"])
    _time(item["updated_at"])
    if _time(item["updated_at"]) < _time(item["created_at"]):
        _fail("Update precedes creation.")
    _string(item["origin"], "origin")
    refs = item["source_refs"]
    if not isinstance(refs, list) or not refs or len(refs) > MAX_REFS or len(refs) != item["version"]:
        _fail("Each version needs exactly one accumulated source reference.")
    seen_dialogs = set()
    previous_time = None
    for ref in refs:
        base_fields = {"dialog_id", "message_id", "source", "quote", "time"}
        image_fields = {"evidence_origin", "image_ids"}
        if not isinstance(ref, dict) or set(ref) not in (base_fields, base_fields | image_fields):
            _fail("Invalid source reference fields.", code="invalid_fields", path="source_refs")
        if image_fields <= ref.keys():
            _image_source({key: ref[key] for key in image_fields}, ref["source"])
        _string(ref["dialog_id"], "source dialog ID")
        _enum(ref["source"], {"user", "llm"}, "reference source")
        _string(ref["message_id"], "source message ID", 1000, nullable=ref["source"] == "user")
        _string(ref["quote"], "quote", 1000)
        ref_time = _time(ref["time"])
        if ref["dialog_id"] in seen_dialogs or (previous_time is not None and ref_time < previous_time):
            _fail("Source references must be unique dialogs in chronological order.")
        seen_dialogs.add(ref["dialog_id"])
        previous_time = ref_time
    if refs[0]["time"] != item["created_at"] or refs[-1]["time"] != item["updated_at"]:
        _fail("Reference timestamps disagree with item timestamps.")
    if refs[-1]["source"] != item["source"]:
        _fail("Last reference source disagrees with update source.")
    if "evidence_origin" in refs[-1] and item["domain"] != "uncertain":
        _fail("Image evidence requires uncertain domain.", code="image_requires_uncertain", path="domain")
    if kind == "events" and item["origin"] != refs[0]["source"]:
        _fail("Event origin must match its first evidence source.")


def _context(context):
    if not isinstance(context, dict) or set(context) != {"revision", "applied_dialog_ids", *KINDS}:
        _fail("Invalid story context fields.")
    _integer(context["revision"], "revision")
    _strings(context["applied_dialog_ids"], "applied_dialog_ids", 1000000)
    if context["revision"] != len(context["applied_dialog_ids"]):
        _fail("Story revision must equal applied dialog count.")
    all_ids = set()
    positions = {dialog_id: index for index, dialog_id in enumerate(context["applied_dialog_ids"])}
    for kind in KINDS:
        if not isinstance(context[kind], list):
            _fail("Invalid projection items.")
        for item in context[kind]:
            _fields(item, kind, stored=True)
            if item["id"] in all_ids:
                _fail("Duplicate or cross-kind story ID.")
            all_ids.add(item["id"])
            ref_positions = [positions.get(ref["dialog_id"], -1) for ref in item["source_refs"]]
            if -1 in ref_positions or ref_positions != sorted(ref_positions):
                _fail("References must follow committed dialog order.")
    event_ids = {item["id"] for item in context["events"]}
    for item in context["memories"]:
        if not set(item["related_event_ids"]) <= event_ids:
            _fail("Unknown related event ID.")


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _redact(v, secrets) for k, v in value.items()}
    return value


def validate_updates(raw, *, story_context, user_body, assistant_body, anchors=None, secrets=(),
                     user_evidence=None) -> dict:
    """Validate only the two model arrays; verify original quotes BEFORE redaction.

    anchors is optional program metadata: as_of, received_at,
    received_at_source. Normalized event times must be timezone-aware ISO.
    Memory related_event_ids can only refer to existing context events, not to
    freshly extracted events (whose identifiers have not yet been assigned).
    Optional user_evidence={"text": original_body, "images": [{"image_id":
    "image-1", "text": observation_text}]} replaces user_body for quote checks.
    Prefer original text, otherwise select the first complete single-image match;
    event raw time must occur in that same fragment. Image matches require the
    uncertain domain. Only this validator adds private _validated_source metadata;
    model input must never contain provenance fields. materialize_updates consumes
    that metadata without changing its API or the package schema version.
    """
    try:
        _context(story_context)
    except StoryValidationError as exc:
        raise StoryError("Invalid caller story context.") from exc
    if isinstance(raw, str):
        try:
            raw = _json_loads(raw)
        except ValueError:
            _fail("Extraction is not JSON.")
    if not isinstance(raw, dict) or set(raw) != {"event_updates", "memory_updates"}:
        _fail("Extraction must contain only event_updates and memory_updates.",
              code="invalid_fields" if isinstance(raw, dict) else "invalid_type")
    if user_body is not None and not isinstance(user_body, str):
        _fail("User body must be text or null.")
    if not isinstance(assistant_body, str):
        _fail("Assistant body must be text.")
    if user_evidence is not None:
        _user_evidence(user_evidence)
        if user_body is None:
            _fail("Program user evidence requires a current user body.", path="user_evidence")
    if anchors is None:
        anchors = {}
    if not isinstance(anchors, dict) or anchors.keys() - {"as_of", "received_at", "received_at_source"}:
        _fail("Program anchors must be a time metadata mapping.")
    for key in ("as_of", "received_at"):
        if anchors.get(key) is not None:
            _time(anchors[key], f"anchors.{key}")
    if anchors.get("received_at_source") is not None:
        _enum(anchors["received_at_source"], {"imap_internaldate", "observed_at", "unknown"}, "received_at_source")
    clean_secrets = tuple(s for s in secrets if isinstance(s, str) and s)
    result = copy.deepcopy(raw)
    event_ids = {item["id"] for item in story_context["events"]}
    for kind, key in (("events", "event_updates"), ("memories", "memory_updates")):
        updates = result[key]
        if not isinstance(updates, list) or len(updates) > MAX_UPDATES:
            _fail("Too many updates or not an array.",
                  code="invalid_fields" if isinstance(updates, list) else "invalid_type", path=key)
        existing = {item["id"]: item for item in story_context[kind]}
        seen = set()
        for index, item in enumerate(updates):
            prefix = f"{key}[{index}]"
            # Delay source/domain coupling only for metadata-backed user evidence:
            # every valid but non-uncertain image domain gets the same safe hint.
            metadata_user = user_evidence is not None and isinstance(item, dict) and item.get("source") == "user"
            _update_fields(item, kind, prefix, check_domain=not metadata_user)
            target = item["target_id"]
            if target is not None:
                if target not in existing:
                    _fail("Target must be an existing same-kind ID updated only once per batch.",
                          code="unknown_target", path=f"{prefix}.target_id")
                if target in seen:
                    _fail("Target must be an existing same-kind ID updated only once per batch.",
                          code="duplicate_target", path=f"{prefix}.target_id")
                seen.add(target)
                if kind == "memories" and item["origin"] != existing[target]["origin"]:
                    _fail("Memory origin is immutable.", code="immutable_origin", path=f"{prefix}.origin")
            body = user_body if item["source"] == "user" else assistant_body
            image_source = None
            if metadata_user:
                body = user_evidence["text"]
                if item["evidence"] not in body:
                    body = None
                    for image in user_evidence["images"]:
                        if item["evidence"] in image["text"]:
                            body = image["text"]
                            image_source = {"evidence_origin": "image_observation", "image_ids": [image["image_id"]]}
                            break
            if body is None or item["evidence"] not in body:
                _fail("Evidence must be an exact substring of this source body.",
                      code="evidence_not_in_source", path=f"{prefix}.evidence")
            if image_source is not None and item["domain"] != "uncertain":
                _fail("Image evidence requires uncertain domain.", code="image_requires_uncertain", path=f"{prefix}.domain")
            if metadata_user:
                _update_fields(item, kind, prefix)
            if kind == "events":
                event_time = item["event_time"]
                if event_time["raw"] is not None and event_time["raw"] not in body:
                    _fail("Raw event time must occur in the source body.",
                          code="raw_time_not_in_source", path=f"{prefix}.event_time.raw")
            else:
                item.setdefault("related_event_ids", [])
                if not set(item["related_event_ids"]) <= event_ids:
                    _fail("Related events must already exist in story context.",
                          code="unknown_related_event", path=f"{prefix}.related_event_ids")
            cleaned = _redact(item, clean_secrets)
            _update_fields(cleaned, kind, prefix)
            # Redacting identifiers cannot silently retarget an update/reference.
            if cleaned["target_id"] != target or (kind == "memories" and cleaned["related_event_ids"] != item["related_event_ids"]):
                _fail("A credential overlaps a story identifier.", path=prefix)
            # Add provenance AFTER model-field validation and redaction. These IDs
            # are program-owned locators, not model text or credential-bearing text.
            if image_source is not None:
                cleaned["_validated_source"] = image_source
            updates[index] = cleaned
    return result


def _new_id(dialog_id, kind, index):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dfy/story/{dialog_id}/{kind}/{index}"))


def materialize_updates(validated, *, dialog_id, story_context, observed_at,
                        user_message_id, assistant_message_id) -> dict:
    """Freeze validator output into an absolute replay package.

    _validated_source is trusted validator metadata, never a model-input field;
    it is consumed here and persisted only on the new source reference.
    """
    _context(story_context)
    _string(dialog_id, "dialog_id")
    _time(observed_at)
    _string(user_message_id, "user_message_id", 1000, nullable=True)
    _string(assistant_message_id, "assistant_message_id", 1000)
    if dialog_id in story_context["applied_dialog_ids"]:
        _fail("Dialog already applied.")
    if not isinstance(validated, dict) or set(validated) != {"event_updates", "memory_updates"}:
        _fail("Invalid validated update root.")
    package = {"schema_version": 1, "base_revision": story_context["revision"],
               "dialog_id": dialog_id, "observed_at": observed_at}
    for kind, key in (("events", "event_updates"), ("memories", "memory_updates")):
        updates = validated[key]
        if not isinstance(updates, list) or len(updates) > MAX_UPDATES:
            _fail("Invalid validated updates.")
        existing = {item["id"]: item for item in story_context[kind]}
        package[key] = []
        seen = set()
        for index, update in enumerate(updates):
            image_source = None
            if isinstance(update, dict) and "_validated_source" in update:
                image_source = update["_validated_source"]
                _image_source(image_source, update.get("source"))
                update = {key: value for key, value in update.items() if key != "_validated_source"}
            _fields(update, kind)
            if image_source is not None:
                if update["domain"] != "uncertain":
                    _fail("Image evidence requires uncertain domain.", code="image_requires_uncertain", path="domain")
            target = update["target_id"]
            if target is not None and (target not in existing or target in seen):
                _fail("Invalid or duplicate target.")
            seen.add(target) if target is not None else None
            previous = existing.get(target)
            item = {k: copy.deepcopy(v) for k, v in update.items() if k not in {"target_id", "evidence"}}
            if kind == "memories":
                item.setdefault("related_event_ids", [])
            else:
                item["event_time"]["anchor"] = observed_at
            origin = previous["origin"] if previous else (item["origin"] if kind == "memories" else item["source"])
            if kind == "memories" and item["origin"] != origin:
                _fail("Memory origin is immutable.")
            ref = {"dialog_id": dialog_id,
                   "message_id": user_message_id if item["source"] == "user" else assistant_message_id,
                   "source": item["source"], "quote": update["evidence"], "time": observed_at}
            if image_source is not None:
                ref.update(copy.deepcopy(image_source))
            item.update(id=target if previous else _new_id(dialog_id, kind, index),
                        version=previous["version"] + 1 if previous else 1,
                        created_at=previous["created_at"] if previous else observed_at,
                        updated_at=observed_at, origin=origin,
                        source_refs=copy.deepcopy(previous["source_refs"]) + [ref] if previous else [ref])
            package[key].append(item)
    validate_package(package)
    _apply(story_context, package)
    return package


def validate_package(package) -> dict:
    """Pure structural validation for dialog validation; performs no I/O."""
    if not isinstance(package, dict) or set(package) != {
        "schema_version", "base_revision", "dialog_id", "observed_at", "event_updates", "memory_updates"
    }:
        _fail("Invalid story package fields.")
    if type(package["schema_version"]) is not int or package["schema_version"] != 1:
        _fail("Unsupported story package version.")
    _integer(package["base_revision"], "base_revision")
    _string(package["dialog_id"], "dialog_id")
    _time(package["observed_at"])
    seen = set()
    for kind, key in (("events", "event_updates"), ("memories", "memory_updates")):
        items = package[key]
        if not isinstance(items, list) or len(items) > MAX_UPDATES:
            _fail("Invalid package updates.")
        for index, item in enumerate(items):
            _fields(item, kind, stored=True)
            if item["id"] in seen:
                _fail("Duplicate package ID.")
            seen.add(item["id"])
            if item["version"] == 1 and item["id"] != _new_id(package["dialog_id"], kind, index):
                _fail("New item ID is not deterministic.")
            if kind == "events" and item["event_time"]["anchor"] != package["observed_at"]:
                _fail("Event time anchor must be the program observation time.")
            ref = item["source_refs"][-1]
            if ref["dialog_id"] != package["dialog_id"] or ref["time"] != package["observed_at"]:
                _fail("Package source reference is not bound to its dialog/time.")
    return package


def _apply(context, package):
    validate_package(package)
    if package["base_revision"] != context["revision"] or package["dialog_id"] in context["applied_dialog_ids"]:
        _fail("Story package base revision conflicts with committed history.")
    result = copy.deepcopy(context)
    all_ids = {item["id"] for kind in KINDS for item in result[kind]}
    existing_event_ids = {item["id"] for item in context["events"]}
    for kind, key in (("events", "event_updates"), ("memories", "memory_updates")):
        positions = {item["id"]: index for index, item in enumerate(result[kind])}
        for item in package[key]:
            if kind == "memories" and not set(item["related_event_ids"]) <= existing_event_ids:
                _fail("Related events must exist before this package.")
            index = positions.get(item["id"])
            if index is None:
                if item["version"] != 1 or item["id"] in all_ids:
                    _fail("Unknown existing target or cross-kind ID collision.")
                result[kind].append(copy.deepcopy(item))
                all_ids.add(item["id"])
            else:
                old = result[kind][index]
                if (item["version"] != old["version"] + 1 or item["created_at"] != old["created_at"]
                        or item["origin"] != old["origin"] or item["source_refs"][:-1] != old["source_refs"]
                        or _time(item["updated_at"]) < _time(old["updated_at"])):
                    _fail("Absolute update changed prior history/version/origin.")
                result[kind][index] = copy.deepcopy(item)
    result["applied_dialog_ids"].append(package["dialog_id"])
    result["revision"] += 1
    _context(result)
    return result


def _projection(context, kind):
    return {"schema_version": 1, "revision": context["revision"],
            "applied_dialog_ids": context["applied_dialog_ids"], kind: context[kind]}


def _read_projection(path):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    if not text.strip():
        return None
    try:
        return _json_loads(text)
    except ValueError:
        raise StoryError("Story projection is not valid JSON; original retained.") from None


def _atomic_write(path, data):
    name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
            name = file.name
            file.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


def reconcile_locked(dialog_data, directory) -> dict:
    """Verify BOTH projection prefixes before either write, then repair lagging files.

    Caller owns the dialog lock, checks pending mail, and supplies validated dialog
    data. Non-sent records are never applied, even if they contain a package.
    Empty/missing projections mean the zero-length prefix. A failed second replace
    is recoverable on the next call; never resend mail in response to StoryError.
    """
    try:
        if not isinstance(dialog_data, dict) or not isinstance(dialog_data.get("dialogs"), list):
            _fail("Invalid dialog data.")
        contexts = [empty_context()]
        seen_dialogs = set()
        for record in dialog_data["dialogs"]:
            if not isinstance(record, dict):
                _fail("Invalid dialog record.")
            if record.get("status") != "sent" or "story_updates" not in record:
                continue
            package = record["story_updates"]
            validate_package(package)
            if record.get("id") != package["dialog_id"] or record["id"] in seen_dialogs:
                _fail("Story package dialog binding conflicts.")
            seen_dialogs.add(record["id"])
            for key in ("event_updates", "memory_updates"):
                for item in package[key]:
                    ref = item["source_refs"][-1]
                    message_key = "user_message_id" if ref["source"] == "user" else "assistant_message_id"
                    if ref["message_id"] != record.get(message_key):
                        _fail("Story reference Message-ID does not match its dialog.")
                    if ref["source"] == "user" and record.get("type") == "deepseek自己主动发送":
                        _fail("Proactive mail cannot contain user evidence.")
            contexts.append(_apply(contexts[-1], package))
        final = contexts[-1]
        directory = Path(directory)
        pending = []
        for kind, filename in (("events", "event.json"), ("memories", "memory.json")):
            path = directory / filename
            data = _read_projection(path)
            if data is not None:
                if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
                    _fail("Invalid projection schema version.")
                revision = data.get("revision")
                if type(revision) is not int or not 0 <= revision < len(contexts):
                    _fail("Projection revision is outside the committed log.")
                if not _same(data, _projection(contexts[revision], kind)):
                    _fail("Projection is not an exact committed log prefix.")
            target = _projection(final, kind)
            if data != target:
                pending.append((path, target))
        if pending:
            directory.mkdir(parents=True, exist_ok=True)
        for path, target in pending:
            _atomic_write(path, target)
        return copy.deepcopy(final)
    except StoryValidationError as exc:
        raise StoryError("Story history/projection validation failed; stop and retain originals.") from exc
    except (OSError, UnicodeError) as exc:
        raise StoryError("Story projection I/O failed; retain dialog log and retry reconciliation, not delivery.") from exc
