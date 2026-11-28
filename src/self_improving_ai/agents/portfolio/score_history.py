"""Replay the scoring effect of each recorded delivery using the current rules."""
import json
from collections import defaultdict
from zoneinfo import ZoneInfo

from self_improving_ai.config import settings
from .preference_learning.learner import load_rules, rebuild_state
from .preference_learning.schemas import canonical, timestamp
from .risk_scoring import risk_index


def _time(value):
    try:
        return timestamp(value)
    except ValueError:
        return None


def _index_by_scope(client, history, rules, calibration, work_date):
    rebuilt = rebuild_state({"client_id": client}, history, rules)
    groups = {"null": []}
    for observation in rebuilt["risk_observations"]:
        groups.setdefault(canonical(observation.get("scope")), []).append(observation)
    return {scope: risk_index(rows, calibration if scope == "null" else {}, work_date=work_date)
            for scope, rows in groups.items()}


def _changes(before, after):
    neutral = {"effective": 50, "reference": 50,
               "breakdown": {"anchor": 50, "trial_points": 0, "behavior_points": 0, "current_points": 0}}
    result = []
    for scope in dict.fromkeys(["null", *before, *after]):
        old, new = before.get(scope, neutral), after.get(scope, neutral)
        parts = {}
        for part in neutral["breakdown"]:
            delta = new["breakdown"][part] - old["breakdown"][part]
            if abs(delta) >= 1e-9:
                parts[part] = delta
        if scope == "null" or parts or old["effective"] != new["effective"]:
            result.append({"scope": json.loads(scope), "components": parts,
                           "effective": [old["effective"], new["effective"]],
                           "reference": [old["reference"], new["reference"]]})
    return result


def replay_score_history(client, deliveries, calibrations, visible_events):
    if not visible_events:
        return {}
    timeline = []
    last_recorded_time = None
    for row in deliveries:
        recorded = _time(row["occurred_at"])
        processed = _time(row["processed_at"])
        if recorded is not None:
            last_recorded_time = max(recorded, last_recorded_time or recorded)
        if processed is not None:
            last_recorded_time = max(processed, last_recorded_time or processed)
        at = processed or last_recorded_time
        if at and (row["kind"] == "retract" or row["extraction"]):
            timeline.append((at, row["seq"], row))
    for calibration in calibrations:
        at = _time(calibration["created"])
        if at:
            timeline.append((at, 0, calibration))
    timeline.sort(key=lambda step: step[:2])

    rules = load_rules()
    zone = ZoneInfo(settings.portfolio_work_timezone)
    history = {"extractions": [], "retracted_event_ids": []}
    calibration = {}
    results = defaultdict(list)
    for at, _, row in timeline:
        work_date = at.astimezone(zone).date()
        if "kind" not in row.keys():
            baseline = _index_by_scope(client, history, rules, {}, work_date)["null"]["reference"]
            calibration = {"chosen": row["chosen"], "mode": row["mode"], "baseline": baseline}
            continue
        event_id = row["event"]
        tracked = event_id in visible_events
        if tracked:
            before = _index_by_scope(client, history, rules, calibration, work_date)
        if row["kind"] == "retract":
            history["retracted_event_ids"].append(event_id)
        else:
            history["extractions"].append(json.loads(row["extraction"]))
        if tracked:
            after = _index_by_scope(client, history, rules, calibration, work_date)
            results[event_id].append({"revision": row["revision"], "kind": row["kind"],
                                      "changes": _changes(before, after)})
    return results
