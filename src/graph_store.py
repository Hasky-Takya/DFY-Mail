"""Disposable deterministic graph projection; dialog/story data remain authoritative.

Only synthetic/caller-supplied data is read. No model calls, dialog writes, or
implicit recovery of a damaged database take place here. Call sync under the
same lock used for the authoritative dialog/story snapshot.
"""

import copy
import hashlib
import importlib
import sys
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
import unicodedata
from datetime import datetime, timezone


PROJECTOR_VERSION = 3
SCHEMA_VERSION = 2
DB_VERSION = 1
DB_FILENAME = "graph_store.sqlite3"
MANIFEST_FILENAME = "graph_migration_manifest.json"
_COLLECTIONS = ("events", "profiles", "preferences", "memories", "facts", "entities",
                "relations", "states", "event_relations")
_TABLES = ("entities", "facts", "assertions", "states", "source_items", "projection_commits")
_ERROR = "Graph projection unavailable; authoritative data was not changed."


class GraphStoreError(RuntimeError):
    """Controlled error; never includes source content, SQL, or a local path."""


def _fail():
    logging.getLogger("dfy.graph").warning("graph_projection_unavailable code=invalid_projection")
    raise GraphStoreError(_ERROR) from None


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_dump(value).encode("utf-8")).hexdigest()


def _loads(text):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                _fail()
            value[key] = item
        return value

    def constant(_):
        _fail()
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (TypeError, ValueError, RecursionError):
        _fail()


def _private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _atomic_json(path, value):
    text = _dump(value) + "\n"
    try:
        if path.exists() and path.read_text(encoding="utf-8") == text:
            return
        fd, name = tempfile.mkstemp(prefix=".graph-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            _private(name)
            os.replace(name, path)
            _private(path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    except (OSError, ValueError, TypeError):
        _fail()


def read_manifest(directory):
    """Read the migration switch strictly. Missing means legacy/shadow mode.

    Migration owns the optional state/backups/source_digest fields. This helper
    never follows any backup path and cannot be redirected by an enabled flag.
    """
    path = Path(directory) / MANIFEST_FILENAME
    try:
        if not path.exists():
            return {"schema_version": 1, "mode": "shadow"}
        value = _loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict)
                or not {"schema_version", "mode"} <= value.keys()
                or value.keys() - {"schema_version", "mode", "state", "backups", "source_digest"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["mode"] not in ("shadow", "active")):
            _fail()
        if "state" in value and (not isinstance(value["state"], str) or not value["state"]):
            _fail()
        if "backups" in value and not isinstance(value["backups"], dict):
            _fail()
        if "source_digest" in value and (not isinstance(value["source_digest"], str)
                or len(value["source_digest"]) != 64
                or any(char not in "0123456789abcdef" for char in value["source_digest"])):
            _fail()
        return value
    except (OSError, UnicodeError, TypeError, ValueError):
        _fail()


def event_filename(directory):
    """Filename for the *legacy* story event projection, not the graph export."""
    return "event_legacy.json" if read_manifest(directory)["mode"] == "active" else "event.json"


def _core(name="graph_memory"):
    # Tests/tools may load this module by absolute file path, without src on PATH.
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            _fail()
        path = Path(__file__).with_name(name + ".py")
        if not path.is_file():
            _fail()
        parent = str(path.parent)
        sys.path.insert(0, parent)
        try:
            return importlib.import_module(name)
        except (ImportError, ValueError, OSError):
            _fail()
        finally:
            sys.path.remove(parent)


def _bound(value, default, maximum=1000):
    if value is None:
        return default
    if type(value) is not int or value < 0:
        _fail()
    return min(value, maximum)


def _source_ref(kind, item):
    return {"kind": kind, "id": item["id"], "version": item["version"], "digest": _digest(item)}


def _evidence_id(evidence):
    return "evidence-" + _digest(evidence)[:24] if evidence else None


def _identity(kind, item):
    if kind == "entities":
        return (item["domain"], item["type"],
                " ".join(unicodedata.normalize("NFKC", item["name"]).casefold().split()))
    if kind == "facts":
        return item["domain"], item["ref"], item["category"]
    values = (item["domain"], item["subject"], item["predicate"])
    return values if kind == "states" else values + (item["object"],)


def _entity_id(name, domain):
    return "entity-" + _digest(["person", domain, " ".join(name.split()).casefold()])[:24]


def _current(item, as_of):
    if item.get("state") in ("retracted", "expired", "superseded", "inactive", "ended", "closed"):
        return False
    if item.get("status") in ("retracted", "expired", "superseded", "inactive", "deleted", "archived"):
        return False
    until = item.get("valid_until")
    if until is not None and as_of is not None:
        try:
            end = datetime.fromisoformat(until.replace("Z", "+00:00"))
            now = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if end.tzinfo is None or now.tzinfo is None:
                _fail()
            if end <= now:
                return False
        except (TypeError, ValueError, AttributeError):
            _fail()
    return True


def _ends(item, kind):
    if kind in ("relations", "event_relations"):
        return [item.get("subject"), item.get("object")]
    if kind == "states":
        return [item.get("subject")]
    return []


def _closed(view):
    """Remove assertions whose endpoints/support are not visible, to a fixpoint."""
    while True:
        known = {item["id"] for kind in _COLLECTIONS for item in view[kind]}
        changed = False
        for kind in ("relations", "states", "event_relations"):
            before = view[kind]
            view[kind] = [item for item in before
                          if all(end in known for end in _ends(item, kind))
                          and (not item.get("support_ref") or item["support_ref"] in known)]
            changed |= len(before) != len(view[kind])
        if not changed:
            return view


def _annotations(directory, explicit):
    result = []
    path = directory / "graph_annotations.jsonl"
    try:
        if path.exists():
            # splitlines accepts a final newline, but rejects every blank record.
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    _fail()
                result.append(_loads(line))
        if isinstance(explicit, (str, bytes, dict)):
            _fail()
        result.extend(explicit)
        packages = []
        for value in result:
            if isinstance(value, dict) and set(value) == {"package", "hash"}:
                if not isinstance(value["hash"], str) or value["hash"] != _digest(value["package"]):
                    _fail()
                value = value["package"]
            if not isinstance(value, dict):
                _fail()
            packages.append(copy.deepcopy(value))
        return packages
    except (OSError, UnicodeError, TypeError, ValueError, RecursionError):
        _fail()


def _inputs(dialog_data, context, annotations, settings=None):
    if (not isinstance(dialog_data, dict) or not isinstance(dialog_data.get("dialogs"), list)
            or not isinstance(context, dict)
            or set(context) != {"revision", "events", "memories", "applied_dialog_ids"}
            or type(context["revision"]) is not int or context["revision"] < 0
            or not isinstance(context["applied_dialog_ids"], list)
            or any(not isinstance(value, str) or not value for value in context["applied_dialog_ids"])
            or len(set(context["applied_dialog_ids"])) != len(context["applied_dialog_ids"])):
        _fail()
    sources, sent, seen = {}, [], set()
    for kind in ("events", "memories"):
        if not isinstance(context[kind], list):
            _fail()
        for item in context[kind]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                    or item["id"] in seen or type(item.get("version")) is not int or item["version"] < 1
                    or not isinstance(item.get("content"), str)
                    or not isinstance(item.get("domain"), str)
                    or not isinstance(item.get("people", []), list)
                    or any(not isinstance(name, str) or not name.strip() for name in item.get("people", []))):
                _fail()
            seen.add(item["id"])
            sources[(kind[:-1] if kind == "events" else "memory", item["id"])] = item
    dialogs = set()
    for record in dialog_data["dialogs"]:
        if not isinstance(record, dict):
            _fail()
        if record.get("status") != "sent":
            continue
        if not isinstance(record.get("id"), str) or not record["id"] or record["id"] in dialogs:
            _fail()
        dialogs.add(record["id"])
        sent.append(record)
    # Omit volatile/unrelated fields and unsent drafts from the cache key.
    packets = [{field: row.get(field) for field in
                ("id", "graph_updates", "user_message_id", "assistant_message_id",
                 "user_body", "assistant_body", "observed_at", "updated_at", "story_updates")}
               for row in sent]
    key = _digest({"projector_version": PROJECTOR_VERSION, "story_context": context,
                   "sent_packages": packets, "annotations": annotations,
                   "settings": _core("graph_schema").normalize_settings(settings)})
    return sources, sent, key


def _baseline(context, sources):
    view = {kind: [] for kind in _COLLECTIONS}
    entities, relations = {}, {}
    for (kind, _), item in sources.items():
        ref = _source_ref(kind, item)
        result = {key: copy.deepcopy(value) for key, value in item.items()
                  if key not in {"source_refs", "people", "content"}}
        result.update(text=item["content"], source_ref=ref, projection_pending=True,
                      latest_evidence_id=_evidence_id((item.get("source_refs") or [None])[-1]))
        collection = "events" if kind == "event" else "memories"
        view[collection].append(result)
        if kind == "memory":
            category = item.get("category")
            if item["domain"] == "user_preference" or category == "preference":
                view["preferences"].append(copy.deepcopy(result))
            elif category in ("profile", "identity", "background", "personality"):
                view["profiles"].append(copy.deepcopy(result))
        for name in item.get("people", []):
            name = " ".join(name.split())
            identifier = _entity_id(name, item["domain"])
            entity = entities.setdefault(identifier, {
                "id": identifier, "version": 1, "type": "person", "name": name,
                "aliases": [], "source": item.get("source"), "domain": item["domain"],
                "source_ref": ref, "latest_evidence_id": result["latest_evidence_id"]})
            if name != entity["name"] and name not in entity["aliases"]:
                entity["aliases"].append(name)
            rel_id = "relation-" + _digest([identifier, "mentioned_in", item["id"]])[:24]
            relations[rel_id] = {"id": rel_id, "version": 1, "subject": identifier,
                                 "predicate": "mentioned_in", "object": item["id"],
                                 "support_ref": item["id"], "state": "asserted",
                                 "source": item.get("source"), "domain": item["domain"],
                                 "source_ref": ref, "latest_evidence_id": result["latest_evidence_id"]}
    view["entities"] = list(entities.values())
    view["relations"] = list(relations.values())
    return view


def _normalize_entities(view, raw_graph, settings):
    """Projection-only identity map; representatives are always existing real IDs."""
    schema = _core("graph_schema")
    groups, mapping = {}, {}
    semantic_ids = {identifier for kind, identifier in raw_graph if kind == "entity"}
    for item in view["entities"]:
        name = schema.fixed_person_name(item["name"], settings) if item["type"] == "person" else item["name"]
        key = item["domain"], item["type"], schema.normalize_name(name)
        groups.setdefault(key, []).append(item)
    merged = []
    for (_, _, normalized), items in sorted(groups.items()):
        semantic = [item for item in items if item["id"] in semantic_ids]
        # Distinct semantic records with the same literal name have no uniquely
        # editable target. Do not guess based on a source or a version number.
        literal = [schema.normalize_name(item["name"]) for item in semantic]
        if len(literal) != len(set(literal)) or len({item["source"] for item in semantic}) > 1:
            merged.extend(items)
            continue
        representative = min(items, key=lambda item: (item["id"] not in semantic_ids,
                               schema.normalize_name(item["name"]) != normalized, item["id"]))
        result = copy.deepcopy(representative)
        result["canonical_id"] = representative["id"]
        result["origin_ids"] = sorted(item["id"] for item in items)
        canonical_name = schema.fixed_person_name(result["name"], settings) if result["type"] == "person" else result["name"]
        result["name"] = canonical_name
        result["aliases"] = sorted({name for item in items for name in [item["name"], *item.get("aliases", [])]
                                    if name != canonical_name})
        # Avoid extra projection metadata for an unchanged, unmerged entity.
        if len(items) == 1 and representative["name"] == canonical_name:
            result = copy.deepcopy(representative)
        merged.append(result)
        mapping.update({item["id"]: representative["id"] for item in items})
    view["entities"] = merged
    for kind in ("relations", "states"):
        for item in view[kind]:
            for field in ("subject", "object"):
                if field in item:
                    item[field] = mapping.get(item[field], item[field])
    return mapping


def _project(context, sources, sent, annotations, key, settings=None):
    view = _baseline(context, sources)
    index = {pair: _source_ref(pair[0], item) for pair, item in sources.items()}
    raw_graph, covered, incomplete, commits = {}, set(), set(), []
    # Keep historical canonical versions for replay dependencies. Freshness is a
    # publication decision, not a reason to erase a committed graph ancestor.
    versions = {(ref["kind"], ref["id"], ref["version"], ref["digest"]): item
                for pair, item in sources.items() for ref in [_source_ref(pair[0], item)]}
    for row in sent:
        story_package = row.get("story_updates")
        if story_package is None:
            continue
        try:
            from story_memory import validate_package as validate_story_package
            validate_story_package(story_package)
        except (ValueError, KeyError, TypeError):
            _fail()
        if story_package["dialog_id"] != row["id"]:
            _fail()
        for source_kind, field in (("event", "event_updates"), ("memory", "memory_updates")):
            for item in story_package[field]:
                ref = _source_ref(source_kind, item)
                versions[(source_kind, item["id"], item["version"], ref["digest"])] = item
    lineage, item_sources = {}, {}
    packages = []
    for record in sent:
        package = record.get("graph_updates")
        if package is None:
            incomplete.add(record["id"])
        else:
            packages.append((package, record))
    packages.extend((package, None) for package in annotations)
    # Replay graph-version dependencies, not file order. Backfill annotations can
    # precede later sent edits even though JSONL is read after dialog records.
    # Cycles/missing prerequisites remain stale and are never materialized.
    def replay_key(pair):
        value = pair[0]
        if not isinstance(value, dict):
            _fail()
        return str(value.get("observed_at", "")), str(value.get("package_id", ""))
    waiting, packages, available = sorted(packages, key=replay_key), [], set()
    while waiting:
        ready = []
        for entry in waiting:
            value = entry[0]
            refs = value.get("source_refs", [])
            if not isinstance(refs, list) or any(not isinstance(ref, dict) for ref in refs):
                _fail()
            dependencies = {(ref.get("kind"), ref.get("id"), ref.get("version")) for ref in refs
                            if ref.get("kind") not in ("mail", "event", "memory")}
            if dependencies <= available:
                ready.append(entry)
        if not ready:
            packages.extend(waiting)
            break
        for entry in ready:
            waiting.remove(entry)
            packages.append(entry)
            value = entry[0]
            for kind in ("facts", "entities", "relations", "states", "event_relations"):
                items = value.get(kind, [])
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    _fail()
                singular = "entity" if kind == "entities" else kind[:-1]
                available.update((singular, item.get("id"), item.get("version")) for item in items)
    core = _core() if packages else None
    as_of = max((row.get("observed_at") or row.get("updated_at") or "" for row in sent), default="") or None
    for package, record in packages:
        historical = isinstance(package, dict) and package.get("format") == "dfy.graph_annotation"
        try:
            if historical:
                if record is not None:
                    _fail()
                _core("graph_annotations").validate_annotation(package)
            else:
                bound = record or next((row for row in sent if row["id"] == package.get("dialog_id")), None)
                core.validate_package(package, record=bound)
        except (ValueError, TypeError, KeyError, AttributeError):
            _fail()
        dialog_id = package.get("dialog_id")
        if record is not None and dialog_id != record["id"]:
            _fail()
        as_of = max(filter(None, (as_of, package["observed_at"])))
        if package["status"] != "complete":
            if dialog_id is not None:
                incomplete.add(dialog_id)
            continue
        refs = package["source_refs"]
        fresh = True
        for ref in refs:
            pair = (ref["kind"], ref["id"])
            expected = index.get(pair)
            if ref["kind"] == "mail":
                # Evidence bindings are checked by core against the sent record.
                # Never accept an arbitrary annotation mailbox ref without a
                # matching authoritative sent record and body hash.
                candidates = [record] if record else [row for row in sent if row["id"] == package["dialog_id"]]
                body_matches = False
                for row in candidates:
                    if row is None:
                        continue
                    for body_key, message_key in (("user_body", "user_message_id"),
                                                  ("assistant_body", "assistant_message_id")):
                        if row.get(message_key) == ref["id"] and ref["version"] == 1:
                            body = row.get(body_key)
                            # Raw text is deliberately not required for replay.
                            body_matches = not isinstance(body, str) or _digest(body) == ref["digest"]
                            if body_matches:
                                break
                    if body_matches:
                        break
                if not body_matches:
                    fresh = False
            elif ref["kind"] in ("event", "memory"):
                if (ref["kind"], ref["id"], ref["version"], ref["digest"]) not in versions:
                    fresh = False
            elif expected != ref:
                fresh = False
        if not fresh:
            if record is not None:
                incomplete.add(record["id"])
            continue
        if historical:
            try:
                historical_context = {"events": [], "memories": []}
                for ref in refs:
                    if ref["kind"] in ("event", "memory"):
                        value = versions[(ref["kind"], ref["id"], ref["version"], ref["digest"])]
                        historical_context["events" if ref["kind"] == "event" else "memories"].append(value)
                _core("graph_annotations").validate_annotation(package, story_context=historical_context)
            except (ValueError, TypeError, KeyError, AttributeError):
                _fail()
        current_sources = all(index.get((ref["kind"], ref["id"])) == ref
                              for ref in refs if ref["kind"] in ("event", "memory"))
        if current_sources:
            fact_refs = {item["ref"] for item in package["facts"]}
            covered.update(ref["id"] for ref in refs
                           if ref["kind"] in ("event", "memory") and ref["id"] in fact_refs)
        if dialog_id is not None:
            # Superseded source versions are normal history, not failed
            # extraction. Missing current annotations are reported by pending.
            incomplete.discard(dialog_id)
        commits.append({"id": package["package_id"], "dialog_id": dialog_id,
                        "source_refs": copy.deepcopy(refs)})
        for kind in ("facts", "entities", "relations", "states", "event_relations"):
            for item in package[kind]:
                pair = (kind[:-1] if kind != "entities" else "entity", item["id"])
                if kind == "facts":
                    pair = ("fact", item["id"])
                old = raw_graph.get(pair)
                if (old is None and item["version"] != 1) or (old is not None and
                        (item["version"] > old["version"] + 1 or _identity(kind, item) != _identity(kind, old))):
                    _fail()
                if old is not None and item["version"] <= old["version"]:
                    if item["version"] == old["version"] and item != old:
                        _fail()
                    continue
                raw_graph[pair] = copy.deepcopy(item)
                index[pair] = _source_ref(pair[0], item)
                # Predecessor versions establish lineage, but a new annotation
                # carries its own current source set. Endpoint dependencies still
                # inherit provenance until those endpoints are renewed.
                endpoints = set(_ends(item, kind))
                dependencies = [ref for ref in refs
                                if ref["kind"] not in ("event", "memory", "mail")
                                and ref["id"] in endpoints
                                and (ref["kind"], ref["id"]) != pair]
                provenance = [copy.deepcopy(ref) for ref in refs if ref["kind"] in ("event", "memory")]
                for ref in dependencies:
                    provenance.extend(copy.deepcopy(lineage.get((ref["kind"], ref["id"], ref["version"], ref["digest"]), [])))
                unique = {(_dump(ref)): ref for ref in provenance}
                item_sources[pair] = list(unique.values())
                lineage[(pair[0], item["id"], item["version"], _digest(item))] = item_sources[pair]
    # Latest graph versions win, including explicit retractions. A display item
    # retains its original story ID/version; semantic IDs are annotation_id.
    for (kind, identifier), item in sorted(raw_graph.items()):
        if any(index.get((ref["kind"], ref["id"])) != ref
               for ref in item_sources.get((kind, identifier), [])):
            continue
        mapped = {field: copy.deepcopy(value) for field, value in item.items() if field != "evidence"}
        mapped["latest_evidence_id"] = _evidence_id(item.get("evidence"))
        if kind == "fact":
            mapped["source_ref"] = next((ref for ref in index.values()
                                         if ref["id"] == item["ref"] and ref["kind"] in ("event", "memory")), None)
            view["facts"].append(mapped)
            source_id = item["ref"]
            targets = [row for collection in ("events", "memories", "profiles", "preferences")
                       for row in view[collection] if row["id"] == source_id]
            for row in targets:
                if row["domain"] != item["domain"] or row.get("source") != item["source"]:
                    _fail()
                row.update(text=item["text"], annotation_id=item["id"],
                           latest_evidence_id=mapped["latest_evidence_id"], projection_pending=False)
                if row in view["memories"]:
                    category = item.get("category")
                    for collection in ("profiles", "preferences"):
                        view[collection] = [entry for entry in view[collection] if entry["id"] != source_id]
                    if category in ("profile", "preference"):
                        view["profiles" if category == "profile" else "preferences"].append(copy.deepcopy(row))
            continue
        collection = "entities" if kind == "entity" else kind + "s"
        if kind == "event_relation":
            collection = "event_relations"
        view[collection] = [row for row in view[collection] if row["id"] != item["id"]]
        if _current(mapped, as_of):
            view[collection].append(mapped)
    # Pre-graph dialogs can be covered by historical annotations. Explicit
    # incomplete packages remain incomplete; do not erase a known extraction
    # failure merely because another package mentioned the same source.
    for record in sent:
        if record.get("graph_updates") is not None or record["id"] not in incomplete:
            continue
        affected = {item["id"] for item in sources.values()
                    if any(isinstance(ref, dict) and ref.get("dialog_id") == record["id"]
                           for ref in item.get("source_refs", []))}
        story_package = record.get("story_updates")
        if isinstance(story_package, dict):
            affected.update(item["id"] for field in ("event_updates", "memory_updates")
                            for item in story_package.get(field, []) if isinstance(item, dict)
                            and item.get("id") in {value["id"] for value in sources.values()})
        if affected <= covered:
            incomplete.discard(record["id"])
    endpoint_types = {identifier: item.get("type") for (kind, identifier), item in raw_graph.items() if kind == "entity"}
    endpoint_types.update({identifier: "event" for kind, identifier in sources if kind == "event"})
    schema = _core("graph_schema")
    rejected = set()
    for kind in ("relations", "event_relations"):
        accepted = []
        for item in view[kind]:
            code = None if item["predicate"] == "mentioned_in" else schema.assertion_error(kind, item, endpoint_types)
            if code:
                rejected.add(item["id"])
                logging.getLogger("dfy.graph").warning("graph_projection_rejected code=%s", code)
            else:
                accepted.append(item)
        view[kind] = accepted
    _normalize_entities(view, raw_graph, settings or schema.normalize_settings())
    # Alias normalization can expose a historical self-edge even when raw IDs differ.
    for kind in ("relations", "event_relations"):
        for item in view[kind]:
            if item["subject"] == item["object"]:
                rejected.add(item["id"])
        view[kind] = [item for item in view[kind] if item["id"] not in rejected]
    pending = sorted(item["id"] for item in sources.values() if item["id"] not in covered)
    for kind in _COLLECTIONS:
        for item in view[kind]:
            if item["id"] in covered:
                item["projection_pending"] = False
        view[kind].sort(key=lambda item: item["id"])
    _closed(view)
    view.update(schema_version=SCHEMA_VERSION, format="dfy.compact_event_graph",
                generation=key, source_digest=_digest(context),
                source_watermark={"revision": context["revision"],
                                  "applied_dialog_ids": copy.deepcopy(context["applied_dialog_ids"])},
                coverage={"status": "partial" if pending or incomplete or rejected else "complete",
                          "pending_source_ids": pending, "incomplete_dialog_ids": sorted(incomplete),
                          "rejected_assertions_count": len(rejected),
                          "rejected_assertion_ids": sorted(rejected)[:200]},
                projector_version=PROJECTOR_VERSION, as_of=as_of)
    # Render before any database transaction or export replacement.
    _dump(view)
    canonical = {kind: [] for kind in ("facts", "entities", "relations", "states", "event_relations")}
    visible = {identifier for kind in _COLLECTIONS for item in view[kind]
               for identifier in [item["id"], *item.get("origin_ids", [])]}
    for (kind, identifier), item in sorted(raw_graph.items()):
        collection = "entities" if kind == "entity" else "event_relations" if kind == "event_relation" else kind + "s"
        if identifier in visible:
            canonical[collection].append(item)
    return view, commits, canonical


class GraphStore:
    def __init__(self, directory, settings=None):
        self.directory = Path(directory)
        if settings is not None and not isinstance(settings, dict):
            _fail()
        try:
            self.settings = _core("graph_schema").normalize_settings(settings)
        except ValueError:
            _fail()
        self._transient = None
        self.path = self.directory / DB_FILENAME
        self._db = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            existed = self.path.exists()
            if existed and self.path.stat().st_size == 0:
                _fail()
            self._db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            self._db.execute("PRAGMA busy_timeout=10000")
            self._db.execute(f"PRAGMA cache_size=-{self.settings['sqlite_cache_kib']}")
            self._db.execute("PRAGMA temp_store=FILE")
            if self._db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                _fail()
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if existed:
                if version != DB_VERSION:
                    _fail()
                names = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not set(_TABLES + ("metadata", "view")) <= names:
                    _fail()
                # Validate the cache itself rather than silently replacing it.
                self.export()
            else:
                self._db.execute("BEGIN IMMEDIATE")
                try:
                    self._db.execute("CREATE TABLE metadata (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    self._db.execute('CREATE TABLE view (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    for table in _TABLES:
                        self._db.execute(f'CREATE TABLE {table} (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    self._db.execute(f"PRAGMA user_version={DB_VERSION}")
                    self._db.execute("COMMIT")
                except BaseException:
                    self._db.execute("ROLLBACK")
                    raise
            _private(self.path)
        except (sqlite3.Error, OSError, ValueError, TypeError, GraphStoreError):
            self.close()
            _fail()

    def _connection(self):
        if self._db is None:
            _fail()
        return self._db

    def sync(self, dialog_data, story_context, annotations=()):
        """Validate/project entirely first; then replace all cache rows atomically."""
        db = self._connection()
        try:
            manifest = read_manifest(self.directory)
            annotations = _annotations(self.directory, annotations)
            sources, sent, key = _inputs(dialog_data, story_context, annotations, self.settings)
            saved = db.execute("SELECT value FROM metadata WHERE id=?", ("input_hash",)).fetchone()
            if saved and saved[0] == key:
                view = self.export()
            else:
                view, commits, canonical = _project(story_context, sources, sent, annotations, key, self.settings)
                tables = {table: {} for table in _TABLES}
                for kind in _COLLECTIONS:
                    table = ("entities" if kind == "entities" else "states" if kind == "states"
                             else "assertions" if kind in ("relations", "event_relations") else "facts")
                    for item in view[kind]:
                        tables[table][item["id"]] = item
                tables["source_items"] = {item["id"]: {"kind": kind, "item": item}
                                          for (kind, _), item in sources.items()}
                tables["projection_commits"] = {item["id"]: item for item in commits}
                rendered = _dump(view)
                rows = {table: [(identifier, _dump(item)) for identifier, item in items.items()]
                        for table, items in tables.items()}
                db.execute("BEGIN IMMEDIATE")
                try:
                    for table in _TABLES:
                        db.execute(f"DELETE FROM {table}")
                        db.executemany(f"INSERT INTO {table}(id,value) VALUES (?,?)", rows[table])
                    db.execute("DELETE FROM view")
                    db.execute("INSERT INTO view(id,value) VALUES (?,?)", ("current", rendered))
                    db.execute("INSERT INTO view(id,value) VALUES (?,?)", ("canonical", _dump(canonical)))
                    db.executemany("INSERT OR REPLACE INTO metadata(id,value) VALUES (?,?)",
                                   (("input_hash", key), ("view_hash", _digest(view)),
                                    ("canonical_hash", _digest(canonical))))
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            _atomic_json(self.directory / "event_v2.json", view)
            if manifest["mode"] == "active":
                _atomic_json(self.directory / "event.json", view)
            return copy.deepcopy(view)
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, RecursionError):
            _fail()

    def _read_cache(self):
        db = self._connection()
        signature = (db.execute("PRAGMA data_version").fetchone()[0], db.total_changes)
        if self._transient is None or self._transient[0] != signature:
            self._transient = (signature, {})
        return self._transient[1]

    def export(self):
        try:
            cache = self._read_cache()
            if "view" in cache:
                return copy.deepcopy(cache["view"])
            row = self._connection().execute("SELECT value FROM view WHERE id=?", ("current",)).fetchone()
            if row is None:
                # A committed cache missing its view is corruption, not empty.
                if self._db.execute("SELECT 1 FROM metadata LIMIT 1").fetchone():
                    _fail()
                return {}
            value = _loads(row[0])
            saved = self._db.execute("SELECT value FROM metadata WHERE id=?", ("input_hash",)).fetchone()
            checksum = self._db.execute("SELECT value FROM metadata WHERE id=?", ("view_hash",)).fetchone()
            if (not checksum or checksum[0] != _digest(value) or not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION
                    or value.get("format") != "dfy.compact_event_graph" or not saved
                    or value.get("generation") != saved[0]
                    or any(not isinstance(value.get(kind), list) for kind in _COLLECTIONS)):
                _fail()
            cache["view"] = value
            return copy.deepcopy(value)
        except (sqlite3.Error, ValueError, TypeError):
            _fail()

    def canonical_graph_context(self, visible_ids=None):
        """Private validator context, never include its evidence in model payloads."""
        try:
            cache = self._read_cache()
            if "canonical" in cache:
                value = copy.deepcopy(cache["canonical"])
            else:
                row = self._connection().execute("SELECT value FROM view WHERE id=?", ("canonical",)).fetchone()
                value = _loads(row[0]) if row else {kind: [] for kind in ("facts", "entities", "relations", "states", "event_relations")}
                saved = self._db.execute("SELECT value FROM metadata WHERE id=?", ("canonical_hash",)).fetchone()
                if saved and (not row or saved[0] != _digest(value)):
                    _fail()
                cache["canonical"] = copy.deepcopy(value)
            if visible_ids is not None:
                if isinstance(visible_ids, (str, bytes, dict)):
                    _fail()
                allowed = set(visible_ids)
                value = {kind: [item for item in items if item["id"] in allowed]
                         for kind, items in value.items()}
            return value
        except (sqlite3.Error, TypeError, ValueError):
            _fail()

    def request_view(self, *, limit=None, max_chars=None, visible_ids=None, scope=None):
        """Bounded recent one-hop display; size is the actual compact JSON length.

        visible_ids is a caller-provided allow-list of currently displayed IDs.
        scope optionally restricts domains. No source content is truncated in the
        middle; an item either fits in full with its endpoints or is omitted.
        """
        limit = _bound(limit, self.settings.get("context_items", 32))
        max_chars = _bound(max_chars, self.settings.get("context_chars", 16000), 1000000)
        full = self.export()
        if not full:
            return {}
        current_time = datetime.now(timezone.utc).isoformat()
        if visible_ids is not None:
            if isinstance(visible_ids, (str, bytes, dict)):
                _fail()
            visible_ids = set(visible_ids)
            if any(not isinstance(value, str) for value in visible_ids):
                _fail()
        if scope is not None:
            if isinstance(scope, str):
                scope = {scope}
            else:
                scope = set(scope)
            if any(not isinstance(value, str) for value in scope):
                _fail()
        result = {key: copy.deepcopy(value) for key, value in full.items() if key not in _COLLECTIONS}
        result.update({kind: [] for kind in _COLLECTIONS})
        watermark = result["source_watermark"]
        applied = watermark.pop("applied_dialog_ids", [])
        watermark.update(applied_dialog_count=len(applied), last_dialog_id=applied[-1] if applied else None)
        coverage = result["coverage"]
        for field in ("pending_source_ids", "incomplete_dialog_ids", "rejected_assertion_ids"):
            values = coverage[field]
            if field != "rejected_assertion_ids":
                coverage[field + "_count"] = len(values)
            coverage[field] = values[:limit]
        result["view"] = {"bounded": True, "limit": limit, "max_chars": max_chars, "evaluated_at": current_time}
        if len(_dump(result)) > max_chars:
            coverage["pending_source_ids"] = []
            coverage["incomplete_dialog_ids"] = []
            coverage["rejected_assertion_ids"] = []
        if len(_dump(result)) > max_chars:
            _fail()
        canonical = self.canonical_graph_context()
        canonical_items = {item["id"]: item for items in canonical.values() for item in items}
        canonical_ids = set(canonical_items)
        # Display normalization must not invent editable endpoints or rename an
        # immutable target. Restore original edge endpoints and expose real
        # alias targets only when a displayed assertion actually needs them.
        entities = {item["id"]: item for item in full["entities"]}
        origins = {identifier: item for item in full["entities"]
                   for identifier in [item["id"], *item.get("origin_ids", [])]}
        for kind in ("relations", "states", "event_relations"):
            for item in full[kind]:
                original = canonical_items.get(item["id"])
                if original is None:
                    continue
                for field in ("subject", "object"):
                    if field in original:
                        item[field] = original[field]
                        identifier = original[field]
                        if identifier in origins and identifier not in entities:
                            target = {key: copy.deepcopy(value) for key, value in canonical_items[identifier].items() if key != "evidence"}
                            target.update(canonical_id=origins[identifier]["id"], target_alias=True)
                            entities[identifier] = target
        for identifier, item in entities.items():
            original = canonical_items.get(identifier)
            if original is not None and item["name"] != original["name"]:
                item["display_name"] = item["name"]
                item["name"] = original["name"]
        full["entities"] = list(entities.values())
        eligible = {}
        for kind in _COLLECTIONS:
            eligible[kind] = {item["id"]: item for item in full[kind]
                              if (kind not in ("entities", "relations", "states", "event_relations")
                                  or item["id"] in canonical_ids)
                              and (scope is None or item.get("domain") in scope)
                              and (visible_ids is None or item["id"] in visible_ids)
                              and _current(item, current_time)}
        # Prefer classified facts over their mirrored memory, avoiding double
        # token accounting for the same canonical source.
        used_sources = set()
        order = ("facts", "events", "preferences", "profiles", "memories", "entities",
                 "relations", "states", "event_relations")
        known_items = {identifier: (kind, item) for kind in order
                       for identifier, item in eligible[kind].items()}
        def recent(item):
            return (item.get("updated_at") or "", item.get("version", 0), item["id"])
        for kind in order:
            for item in sorted(eligible[kind].values(), key=recent, reverse=True):
                if len(result[kind]) >= limit:
                    break
                source = item.get("source_ref", {}).get("id", item["id"])
                if kind in ("facts", "events", "preferences", "profiles", "memories") and source in used_sources:
                    continue
                trial = copy.deepcopy(result)
                known = {entry["id"] for col in _COLLECTIONS for entry in trial[col]}
                needed = _ends(item, kind) + ([item["support_ref"]] if item.get("support_ref") else [])
                valid = True
                for identifier in needed:
                    if identifier in known:
                        continue
                    endpoint = known_items.get(identifier)
                    if endpoint is None or endpoint[0] in ("relations", "states", "event_relations"):
                        valid = False
                        break
                    endpoint_kind, entry = endpoint
                    if len(trial[endpoint_kind]) >= limit:
                        valid = False
                        break
                    trial[endpoint_kind].append(copy.deepcopy(entry))
                    known.add(identifier)
                if not valid:
                    continue
                if item["id"] not in known:
                    trial[kind].append(copy.deepcopy(item))
                if len(_dump(trial)) <= max_chars:
                    result = trial
                    if kind in ("facts", "events", "preferences", "profiles", "memories"):
                        used_sources.add(source)
        return _closed(result)

    def _get(self, table, identifier):
        if not isinstance(identifier, str):
            _fail()
        try:
            row = self._connection().execute(f"SELECT value FROM {table} WHERE id=?", (identifier,)).fetchone()
            return _loads(row[0]) if row else None
        except sqlite3.Error:
            _fail()

    def get_fact(self, id):
        return self._get("facts", id)

    def resolve_entity(self, name):
        if not isinstance(name, str) or not name.strip():
            _fail()
        schema = _core("graph_schema")
        match = schema.normalize_name(name)
        fixed_match = schema.normalize_name(schema.fixed_person_name(name, self.settings))
        return [copy.deepcopy(item) for item in self.export().get("entities", [])
                if any(schema.normalize_name(value) == match or
                       (item["type"] == "person" and schema.normalize_name(schema.fixed_person_name(value, self.settings)) == fixed_match)
                       for value in [item["name"], *item.get("aliases", [])])][:100]

    def _context(self, identifier, entity, limit):
        limit = _bound(limit, self.settings.get("query_limit", 32), 100)
        if not isinstance(identifier, str):
            _fail()
        full = self.export()
        current_time = datetime.now(timezone.utc).isoformat()
        result = {kind: [] for kind in _COLLECTIONS}
        index = {item["id"]: (kind, item) for kind in _COLLECTIONS for item in full.get(kind, [])
                 if _current(item, current_time)}
        if entity:
            identifier = next((item["id"] for item in full.get("entities", [])
                               if identifier in item.get("origin_ids", [])), identifier)
        root = index.get(identifier)
        if root is None or (root[0] != "entities" if entity else root[0] != "events"):
            return result
        result[root[0]].append(copy.deepcopy(root[1]))
        known = {identifier}
        count = 0
        for kind in ("relations", "states", "event_relations"):
            for item in sorted(full[kind], key=lambda item: item["id"]):
                ends = _ends(item, kind)
                if (identifier not in ends or count >= limit or not _current(item, current_time)
                        or any(end not in index for end in ends)
                        or (item.get("support_ref") and item["support_ref"] not in index)):
                    continue
                for end in ends + ([item["support_ref"]] if item.get("support_ref") else []):
                    if end not in known and end in index:
                        col, value = index[end]
                        result[col].append(copy.deepcopy(value))
                        known.add(end)
                result[kind].append(copy.deepcopy(item))
                known.add(item["id"])
                count += 1
        return _closed(result)

    def get_entity_context(self, id, limit=None):
        return self._context(id, True, limit)

    def get_event_context(self, id, limit=None):
        return self._context(id, False, limit)

    def close(self):
        db, self._db = self._db, None
        if db is not None:
            db.close()

    def __enter__(self):
        self._connection()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


def synchronize(directory, dialog_data, story_context, settings=None):
    with GraphStore(directory, settings=settings) as store:
        return store.sync(dialog_data, story_context)
