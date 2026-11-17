"""Pure historical graph annotations; never invent mail provenance or wall time.

One canonical legacy story item is the evidence authority for each call. Its
content and its individual same-source quotes are separate evidence fragments.
"""

import copy
import logging
import re

from graph_memory import (GraphError, KINDS, _SOURCE_KINDS, _fields, _identity,
                          _index, _int, _text, _time, digest, empty_candidate,
                          stable_id, validate_candidate)

_FORMAT = "dfy.graph_annotation"
_KINDS = {"event": "events", "events": "events", "memory": "memories", "memories": "memories"}


def _fail(code="invalid_binding", path="$"):
    error = GraphError(code=code, path=path)
    logging.getLogger("dfy.graph").warning("graph_annotation_rejected code=%s path=%s", error.code, error.path)
    raise error


def _fragments(source):
    fragments = []
    content = source.get("content")
    if isinstance(content, str) and content.strip():
        fragments.append(("legacy_item", content))
    refs = source.get("source_refs", [])
    if not isinstance(refs, list):
        _fail("invalid_type", "source_refs")
    for ref in refs:
        if isinstance(ref, dict) and ref.get("source") == source.get("source"):
            quote = ref.get("quote")
            if isinstance(quote, str) and quote.strip():
                fragments.append(("legacy_quote", quote))
    return fragments


def _provenance(source, quote):
    _text(quote, "evidence", 1000)
    for origin, fragment in _fragments(source):
        if quote in fragment:
            return {"origin": origin, "source": source["source"], "quote": quote,
                    "source_ref": source["id"], "source_version": source["version"]}
    _fail("evidence_not_in_source", "evidence")


def _source_ref(kind, source):
    return {"kind": _SOURCE_KINDS[kind], "id": source["id"],
            "version": source["version"], "digest": digest(source)}


def _package_id(package):
    return stable_id("annotation", {key: value for key, value in package.items() if key != "package_id"})


def freeze_annotation(raw, *, source_item, kind, story_context, graph_context=None, settings=None):
    """Validate ordinary five-array candidates against one legacy source item.

    kind accepts event/memory or events/memories. No E1/M1 aliases, current mail,
    synthetic timestamps, or cross-fragment evidence are accepted. Existing graph
    targets follow the same displayed-ID/version/immutable-identity rules as live
    extraction. The result is content-addressed and independent of input mutation.
    """
    if not isinstance(kind, str) or kind not in _KINDS:
        _fail("invalid_enum")
    canonical_kind = _KINDS[kind]
    story = _index(story_context, ("events", "memories"))
    if not isinstance(source_item, dict):
        _fail("invalid_type")
    identifier = source_item.get("id")
    _text(identifier, "id")
    entry = story.get(identifier)
    if entry is None or entry[0] != canonical_kind or digest(entry[1]) != digest(source_item):
        _fail()
    _time(source_item.get("updated_at"), "observed_at")
    fragments = _fragments(source_item)
    # Parsing/shape/type diagnostics remain the canonical candidate validator's
    # responsibility; individual-fragment checks happen before its joined corpus.
    from graph_memory import _parse
    parsed = _parse(raw)
    if not isinstance(parsed, dict) or set(parsed) != set(KINDS):
        _fail("invalid_fields")
    origins = {}
    for item_kind in KINDS:
        if not isinstance(parsed[item_kind], list):
            _fail("invalid_type", item_kind)
        for i, item in enumerate(parsed[item_kind]):
            if not isinstance(item, dict):
                _fail("invalid_type", item_kind)
            if item.get("source") != source_item.get("source") or item.get("domain") != source_item.get("domain"):
                _fail("source_domain_mismatch", f"{item_kind}[{i}]")
            origins[(item_kind, i)] = _provenance(source_item, item.get("evidence"))
    corpus = "\n".join(fragment for _, fragment in fragments)
    validated = validate_candidate(parsed, story_context=story_context, graph_context=graph_context,
                                   user_body=corpus if source_item.get("source") == "user" else None,
                                   assistant_body=corpus if source_item.get("source") == "llm" else "",
                                   settings=settings)
    bindings = validated["_bindings"]
    sources = {(ref["kind"], ref["id"]): copy.deepcopy(ref) for ref in bindings["sources"]}
    source_ref = _source_ref(canonical_kind, source_item)
    sources[(source_ref["kind"], identifier)] = source_ref
    package = {"format": _FORMAT, "schema_version": 1, "observed_at": source_item["updated_at"],
               "status": "complete", "reason": None, "source_refs": [], **empty_candidate()}
    for item_kind in KINDS:
        for i, update in enumerate(validated[item_kind]):
            item = {key: copy.deepcopy(value) for key, value in update.items()
                    if key not in {"target_id", "expected_version"} and not (item_kind == "entities" and key == "ref")}
            for key in ("ref", "subject", "object", "support_ref"):
                value = item.get(key)
                if isinstance(value, str) and re.fullmatch(r"[EM][1-9][0-9]*", value):
                    _fail("unknown_reference", f"{item_kind}[{i}]")
            item_id = update.get("target_id") or stable_id(_SOURCE_KINDS[item_kind], *_identity(item_kind, item))
            previous = bindings["graph"].get(item_id)
            item.update(id=item_id, version=previous["version"] + 1 if previous else 1,
                        evidence=origins[(item_kind, i)])
            if previous is not None:
                prior_ref = _source_ref(item_kind, previous)
                sources[(prior_ref["kind"], item_id)] = prior_ref
            package[item_kind].append(item)
    package["source_refs"] = sorted(sources.values(), key=lambda ref: (ref["kind"], ref["id"]))
    package["package_id"] = _package_id(package)
    return validate_annotation(package, story_context=story_context)


def validate_annotation(package, story_context=None):
    """Check the history schema/hash; optional canonical story context binds quotes.

    No online-mail conversion is performed. Graph-source digest freshness and
    previous graph identities remain the projector's responsibility.
    """
    expected = {"format", "schema_version", "package_id", "observed_at", "status", "reason", "source_refs", *KINDS}
    if not isinstance(package, dict) or set(package) != expected:
        _fail("invalid_package")
    if package["format"] != _FORMAT or type(package["schema_version"]) is not int or package["schema_version"] != 1:
        _fail("invalid_package")
    if package["status"] != "complete" or package["reason"] is not None:
        _fail("invalid_package")
    _time(package["observed_at"], "observed_at")
    if package["package_id"] != _package_id(package):
        _fail()
    refs = package["source_refs"]
    if not isinstance(refs, list) or not refs or len(refs) > 1024:
        _fail("invalid_fields", "source_refs")
    sources = {}
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"kind", "id", "version", "digest"}:
            _fail("invalid_fields", "source_refs")
        if not isinstance(ref["kind"], str) or ref["kind"] not in _SOURCE_KINDS.values():
            _fail("invalid_enum", "source_refs")
        _text(ref["id"], "source_refs")
        _int(ref["version"], "source_refs")
        if not isinstance(ref["digest"], str) or re.fullmatch(r"[0-9a-f]{64}", ref["digest"]) is None:
            _fail()
        key = (ref["kind"], ref["id"])
        if key in sources:
            _fail("duplicate_identity")
        sources[key] = ref
    story = _index(story_context, ("events", "memories")) if story_context is not None else None
    if story is not None:
        for (kind, identifier), ref in sources.items():
            if kind in {"event", "memory"}:
                source = story.get(identifier)
                if source is None or _source_ref(source[0], source[1]) != ref:
                    _fail()
    seen = {}
    evidence_sources = set()
    total = 0
    for kind in KINDS:
        if not isinstance(package[kind], list):
            _fail("invalid_type", kind)
        total += len(package[kind])
        for i, item in enumerate(package[kind]):
            path = f"{kind}[{i}]"
            _fields(item, kind, path, 4000, stored=True)
            if item["id"] in seen:
                _fail("duplicate_identity", path)
            seen[item["id"]] = kind
            if item["version"] == 1 and item["id"] != stable_id(_SOURCE_KINDS[kind], *_identity(kind, item)):
                _fail()
            if item["version"] > 1:
                prior = sources.get((_SOURCE_KINDS[kind], item["id"]))
                if prior is None or prior["version"] + 1 != item["version"]:
                    _fail("version_conflict", path)
            evidence = item["evidence"]
            if not isinstance(evidence, dict) or set(evidence) != {"origin", "source", "quote", "source_ref", "source_version"}:
                _fail("invalid_fields", path + ".evidence")
            if evidence["origin"] not in ("legacy_item", "legacy_quote") or evidence["source"] != item["source"]:
                _fail()
            _text(evidence["quote"], path + ".evidence", 1000)
            _text(evidence["source_ref"], path + ".evidence")
            _int(evidence["source_version"], path + ".evidence")
            evidence_sources.add((evidence["source_ref"], evidence["source_version"]))
            matching = [sources[key] for key in (("event", evidence["source_ref"]), ("memory", evidence["source_ref"])) if key in sources]
            if len(matching) != 1 or matching[0]["version"] != evidence["source_version"]:
                _fail()
            if story is not None:
                source = story[evidence["source_ref"]][1]
                if source.get("source") != item["source"] or source.get("domain") != item["domain"]:
                    _fail("source_domain_mismatch", path)
                if source.get("updated_at") != package["observed_at"] or _provenance(source, evidence["quote"]) != evidence:
                    _fail()
    if total > 64 or len(evidence_sources) > 1:
        _fail("invalid_package")
    if total == 0:
        # Even a no-op annotation describes exactly one historical authority;
        # arbitrary extra refs must not masquerade as completed source coverage.
        if len(sources) != 1 or next(iter(sources))[0] not in {"event", "memory"}:
            _fail("invalid_package")
        if story is not None:
            identifier = next(iter(sources))[1]
            if story[identifier][1].get("updated_at") != package["observed_at"]:
                _fail()
    for kind in KINDS:
        for item in package[kind]:
            checks = []
            if kind == "facts":
                checks.append((item["ref"], {"event", "memory"}))
            elif kind != "entities":
                allowed = {"event"} if kind == "event_relations" else {"entity"}
                checks.append((item["subject"], allowed))
                if kind != "states":
                    object_kinds = allowed
                    if kind == "relations" and item["predicate"] in {"participates_in", "organizes"}:
                        object_kinds = {"entity", "event"}
                    checks.append((item["object"], object_kinds))
                if item["support_ref"] is not None:
                    checks.append((item["support_ref"], {"event", "memory"}))
            for identifier, allowed in checks:
                if _SOURCE_KINDS.get(seen.get(identifier)) not in allowed and not any((source_kind, identifier) in sources for source_kind in allowed):
                    _fail("unknown_reference")
    return package
