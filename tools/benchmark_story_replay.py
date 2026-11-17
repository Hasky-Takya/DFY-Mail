"""Offline synthetic replay memory benchmark; no mail, network or real memory.

python -B tools/benchmark_story_replay.py --revisions 600
Reports Python tracemalloc, NOT total Linux RSS or a production capacity promise.
"""
import argparse
import gc
import json
from pathlib import Path
import sys
import time
import tracemalloc
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import story_memory as story


def generate(count):
    current = story.empty_context()
    records = []
    body = "Synthetic plan for a quiet game test."
    for index in range(count):
        raw = {"event_updates": [{"target_id": None, "title": "Synthetic plan", "content": body,
                                  "source": "user", "domain": "shared_story", "importance": 3,
                                  "people": ["Synthetic"], "status": "planned", "tense": "future",
                                  "event_time": {"raw": None, "start": None, "end": None}, "evidence": body}],
               "memory_updates": []}
        validated = story.validate_updates(raw, story_context=current, user_body=body, assistant_body="")
        identifier = "synthetic-" + str(index)
        package = story.materialize_updates(validated, dialog_id=identifier, story_context=current,
                                             observed_at="2026-01-01T00:00:00+00:00",
                                             user_message_id="<synthetic-user>", assistant_message_id="<synthetic-assistant>")
        current = story._apply(current, package)
        records.append({"id": identifier, "status": "sent", "type": "用户主动发送",
                        "user_message_id": "<synthetic-user>", "assistant_message_id": "<synthetic-assistant>",
                        "story_updates": package})
    return {"dialogs": records}, current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revisions", type=int, default=300)
    args = parser.parse_args()
    if not 1 <= args.revisions <= 3000:
        parser.error("revisions must be 1..3000")
    data, expected = generate(args.revisions)
    projections = {kind: story._projection(expected, kind) for kind in ("events", "memories")}
    del expected
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter()
    result = story.replay_checked(data, projections)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps({"revisions": args.revisions, "events": len(result["events"]),
                      "replay_seconds": round(elapsed, 3), "python_peak_mib": round(peak / 1048576, 3),
                      "source_generation_included": False, "metric": "tracemalloc; not total RSS"}, indent=2))


if __name__ == "__main__":
    main()
