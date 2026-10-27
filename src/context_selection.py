"""Pure, request-only recent-context selection; never reads or rewrites history.

The compact story view is deliberately NOT a canonical story_memory context.
Keep the original context for validation/materialization; this module only selects
model-visible items. A future retrieval policy can replace the recent-item policy.
"""

import copy
from datetime import datetime, timezone


RECENT_LIMIT = 20
DIALOG_FIELDS = (
    "id", "type", "received_at", "received_at_source", "sent_at", "user_summary",
    "assistant_summary", "user_message_id", "assistant_message_id",
    "reply_to_assistant_message_id", "reply_relation_source", "image_observations", "subject",
)


class BudgetExceeded(ValueError):
    """Required request content cannot fit even after optional context is removed."""


def _timestamp(item):
    for key in ("updated_at", "sent_at", "received_at", "observed_at"):
        value = item.get(key)
        if value is not None:
            return datetime.fromisoformat(value)
    return datetime.min.replace(tzinfo=timezone.utc)


def dialog_view(record):
    """No persisted story packages, arbitrary metadata, or full source chains."""
    entry = {key: copy.deepcopy(record[key]) for key in DIALOG_FIELDS if key in record}
    entry["initiated_by"] = "assistant" if record.get("type") == "deepseek自己主动发送" else "user"
    return entry


def compact_story(context):
    result = {"revision": context["revision"], "view": "compact", "events": [], "memories": []}
    for kind in ("events", "memories"):
        for item in context[kind]:
            view = {key: copy.deepcopy(value) for key, value in item.items() if key != "source_refs"}
            view["source_refs"] = copy.deepcopy(item["source_refs"][-1:])
            result[kind].append(view)
    return result


def select_context(payload, *, limit, render, system_chars=0, recent_limit=RECENT_LIMIT):
    """Return (selected payload, exact rendered request).

    render must apply the same serialization/redaction used for the actual model
    request. The budget includes selection metadata and external system text.
    All inputs remain unchanged. Required current text, templates, state facts,
    anchors, and repair.error are never truncated. Only optional lists and a
    repair previous_output (when accompanied by an error) may be omitted.
    """
    selected = copy.deepcopy(payload)
    groups = {"dialogs": selected.setdefault("history", [])}
    story = selected.get("story_context")
    if story is not None:
        groups.update(events=story["events"], memories=story["memories"])
    state = selected.get("conversation_state")
    if state is not None:
        for key in ("known_reply_relations", "observed_user_activity"):
            groups[key] = state[key]
    metadata = {"policy": "recent", "recent_limit": recent_limit, "budget_chars": limit}
    for name, items in groups.items():
        total = len(items)
        items.sort(key=_timestamp)
        if len(items) > recent_limit:
            del items[:len(items) - recent_limit]
        metadata[name] = {"total": total, "selected": len(items), "omitted": total - len(items)}
    repair = selected.get("repair")
    optional_previous = isinstance(repair, dict) and "error" in repair and "previous_output" in repair
    if optional_previous:
        metadata["repair_previous_output_omitted"] = False
    selected["selection"] = metadata
    while True:
        # Links are useful only when the actual event item was displayed. Keep
        # canonical related_event_ids untouched outside this request-only copy.
        if story is not None:
            event_ids = {item["id"] for item in story["events"]}
            for item in story["memories"]:
                item["related_event_ids"] = [value for value in item.get("related_event_ids", []) if value in event_ids]
        text = render(selected)
        if len(text) + system_chars <= limit:
            return selected, text
        if optional_previous:
            repair.pop("previous_output")
            metadata["repair_previous_output_omitted"] = True
            optional_previous = False
            continue
        remaining = [(name, items) for name, items in groups.items() if items]
        if not remaining:
            raise BudgetExceeded("Required current content exceeds the request character budget.")
        name, items = min(remaining, key=lambda pair: _timestamp(pair[1][0]))
        del items[0]
        metadata[name]["selected"] -= 1
        metadata[name]["omitted"] += 1
