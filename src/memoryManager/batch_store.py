"""Read and commit the paired v2 story batch logs under the caller's lock.

The dialog log supplies the ordered sent owners. Only a frozen package from a
confirmed sent task may append a pair or repair a partially replaced pair.
"""

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import story_memory as story


_PROJECTIONS = (("event.json", "event_updates"), ("memory.json", "memory_updates"))
_METADATA = ("base_revision", "dialog_id", "observed_at")
_MISSING = object()
_PROACTIVE = "deepseek自己主动发送"


class BatchStoreError(story.StoryError):
    """Invalid or unavailable v2 story state; do not retry email delivery."""


@dataclass(frozen=True)
class BatchState:
    context: dict = field(repr=False)
    _directory: Path = field(repr=False)
    _writes: tuple = field(repr=False)
    _pending: bool = field(repr=False)


def fsync_directory(directory):
    """Add a durability barrier for a previously replaced name on POSIX."""
    if os.name == "posix":
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def atomic_write(path, data):
    """Replace one JSON file using a same-directory, fsynced temporary file.

    On POSIX also fsync the parent directory; on Windows this is an atomic
    replacement but this function does not provide a directory durability barrier.
    """
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = file.name
            json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        temporary = None
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _read(directory, filename, updates_key):
    path = directory / filename
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        # A dangling symlink is not a fresh directory or a missing projection.
        if path.is_symlink():
            raise BatchStoreError(f"{filename} is a dangling link; original retained.") from None
        return _MISSING
    except (OSError, UnicodeError):
        raise BatchStoreError(f"{filename} could not be read as UTF-8; original retained.") from None
    try:
        data = story._json_loads(text)
    except (ValueError, RecursionError):
        raise BatchStoreError(f"{filename} is not valid strict JSON; original retained.") from None
    if (not isinstance(data, dict) or set(data) != {"schema_version", "revision", "batches"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 2
            or type(data["revision"]) is not int or not isinstance(data["batches"], list)
            or data["revision"] != len(data["batches"])):
        raise BatchStoreError(f"{filename} must be a v2 batch log with a matching revision.")
    for index, batch in enumerate(data["batches"]):
        if (not isinstance(batch, dict) or set(batch) != {*_METADATA, updates_key}
                or type(batch["base_revision"]) is not int or batch["base_revision"] != index):
            raise BatchStoreError(f"{filename} batch {index} has invalid fields or base revision.")
    return data


def _pending_batches(package):
    try:
        story.validate_package(package)
    except story.StoryValidationError:
        raise BatchStoreError("The confirmed task's frozen story package is invalid.") from None
    package = copy.deepcopy(package)
    metadata = {key: package[key] for key in _METADATA}
    return (
        dict(metadata, event_updates=package["event_updates"]),
        dict(metadata, memory_updates=[dict(item, source="dialogue", evidence_source=item["source"])
                                      for item in package["memory_updates"]]),
    )


def _propose(logs, pending):
    """Return both proposed logs and a write mask; no filesystem mutations."""
    left, right = (data["revision"] for data in logs)
    if pending is None:
        if left != right:
            raise BatchStoreError("Story log revisions differ without a confirmed pending package.")
        return logs, (False, False)

    base = pending[0]["base_revision"]
    if min(left, right) < base or max(left, right) > base + 1:
        raise BatchStoreError("Confirmed story package does not fit the current log revisions.")
    proposed = []
    changed = []
    for data, batch in zip(logs, pending):
        if data["revision"] == base:
            proposed.append(dict(data, revision=base + 1, batches=[*data["batches"], batch]))
            changed.append(True)
        else:
            try:
                matches = story._same(data["batches"][-1], batch)
            except (ValueError, RecursionError):
                matches = False
            if not matches:
                raise BatchStoreError("The existing story suffix differs from the confirmed package.")
            proposed.append(data)
            changed.append(False)
    return proposed, changed


def _owners(dialogs):
    if isinstance(dialogs, dict) and type(dialogs.get("schema_version")) is int and dialogs["schema_version"] == 2:
        dialogs = dialogs.get("dialogs")
    if not isinstance(dialogs, list):
        raise BatchStoreError("Expected ordered v2 dialog records.")
    owners = {}
    for position, record in enumerate(dialogs):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"].strip():
            raise BatchStoreError("Invalid dialog owner for story replay.")
        if record["id"] in owners:
            raise BatchStoreError("Duplicate dialog owner for story replay.")
        owners[record["id"]] = position, record
    return owners


def _classic_memories(items):
    if not isinstance(items, list) or len(items) > story.MAX_UPDATES:
        raise BatchStoreError("Invalid memory batch updates array.")
    result = []
    for item in items:
        if not isinstance(item, dict) or item.get("source") != "dialogue" or "evidence_source" not in item:
            raise BatchStoreError("A v2 memory update must have a dialogue source and evidence source.")
        classic = dict(item)
        classic["source"] = classic.pop("evidence_source")
        result.append(classic)
    return result


def _replay(logs, owners):
    # Keep insertion order for the latest-by-ID projection, like v1 replay.
    latest = {kind: {} for kind in story.KINDS}
    applied = []
    last_position = -1
    for index, (event_batch, memory_batch) in enumerate(zip(*(data["batches"] for data in logs))):
        if any(event_batch[key] != memory_batch[key] for key in _METADATA):
            raise BatchStoreError(f"Story batch {index} metadata disagrees between the two logs.")
        events = event_batch["event_updates"]
        memories = _classic_memories(memory_batch["memory_updates"])
        try:
            story.validate_package(dict(event_batch, schema_version=1, memory_updates=memories))
        except story.StoryValidationError as exc:
            details = exc.safe_details()
            raise BatchStoreError(f"Story batch {index} has invalid updates or metadata ({details['code']}).") from None

        dialog_id = event_batch["dialog_id"]
        position, owner = owners.get(dialog_id, (-1, {}))
        if owner.get("status") != "sent" or position <= last_position:
            raise BatchStoreError(f"Story batch {index} has no sent dialog owner in committed order.")
        last_position = position
        applied.append(dialog_id)
        # Check memory references before adding any events created by this batch.
        for item in memories:
            if any(event_id not in latest["events"] for event_id in item["related_event_ids"]):
                raise BatchStoreError(f"Story batch {index} refers to an event not present before this batch.")

        for kind, other_kind, items in (("events", "memories", events), ("memories", "events", memories)):
            for item in items:
                ref = item["source_refs"][-1]
                is_user = ref["source"] == "user"
                message_key = "user_message_id" if is_user else "assistant_message_id"
                if (message_key not in owner or owner[message_key] != ref["message_id"]
                        or (is_user and owner.get("type") == _PROACTIVE)):
                    raise BatchStoreError(f"Story batch {index} evidence Message-ID disagrees with its dialog owner.")
                item_id = item["id"]
                previous = latest[kind].get(item_id)
                if item_id in latest[other_kind]:
                    raise BatchStoreError(f"Story batch {index} has a cross-kind ID collision.")
                if previous is None:
                    if item["version"] != 1:
                        raise BatchStoreError(f"Story batch {index} updates an unknown story item.")
                elif (item["version"] != previous["version"] + 1 or item["origin"] != previous["origin"]
                      or item["created_at"] != previous["created_at"]
                      or item["source_refs"][:-1] != previous["source_refs"]):
                    raise BatchStoreError(f"Story batch {index} changes prior story version, origin or evidence.")
                latest[kind][item_id] = item
    context = dict(revision=len(applied), applied_dialog_ids=applied,
                   events=list(latest["events"].values()), memories=list(latest["memories"].values()))
    try:
        story._context(context)
    except story.StoryValidationError:
        raise BatchStoreError("Replayed story context is invalid.") from None
    # Do not share nested item values with the proposed on-disk writes.
    return copy.deepcopy(context)


class BatchStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def load(self, dialogs, pending=None, *, allow_missing=False):
        """Verify the pair and return a write proposal without touching files.

        The caller provides validated v2 dialog records, including the confirmed
        sent owner of the optional v1 package. Only an entirely uninitialized
        standalone store, with no dialogs and no pending package, may pass
        allow_missing=True to propose creating both empty logs.
        """
        owners = _owners(dialogs)
        logs = [_read(self.directory, name, key) for name, key in _PROJECTIONS]
        if any(data is _MISSING for data in logs):
            if allow_missing and not owners and pending is None and all(data is _MISSING for data in logs):
                logs = [{"schema_version": 2, "revision": 0, "batches": []} for _ in _PROJECTIONS]
                changed = (True, True)
            else:
                raise BatchStoreError("The v2 story log pair is missing one or both files; stop and verify the snapshot.")
        else:
            logs, changed = _propose(logs, None if pending is None else _pending_batches(pending))
        context = _replay(logs, owners)
        writes = tuple((name, data) for (name, _), data, change in zip(_PROJECTIONS, logs, changed) if change)
        return BatchState(context, self.directory, writes, pending is not None)

    def commit(self, state):
        """Write only changed files, using a proposal loaded under the same lock."""
        if not isinstance(state, BatchState) or state._directory != self.directory:
            raise BatchStoreError("Invalid story commit proposal for this directory.")
        for name, data in state._writes:
            try:
                atomic_write(self.directory / name, data)
            except (OSError, ValueError, RecursionError):
                raise BatchStoreError(f"{name} could not be committed; retain the confirmed task and do not resend.") from None
        if state._pending and not state._writes:
            # A prior replace may have succeeded just before directory fsync failed.
            try:
                fsync_directory(self.directory)
            except OSError:
                raise BatchStoreError("Story commit directory sync failed; retain the confirmed task and do not resend.") from None
