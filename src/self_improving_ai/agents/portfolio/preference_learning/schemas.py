"""Versioned event validation and JSON-compatible return contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, TypedDict
import json
import math

EVENT_SCHEMA = "portfolio-learning-event-v0"
STATE_SCHEMA = "portfolio-learning-state-v0"
DIMENSIONS = {"risk_tilt": ("conservative", "balanced", "growth"),
              "adjustment_style": ("gradual", "flexible")}
SOURCES = {"default", "manual", "explicit_text", "agent_generated", "unknown"}
OUTCOMES = {"received", "success", "error", "cancelled"}


class ExtractionResult(TypedDict):
    event: dict[str, Any]
    status: str
    signals: list[dict[str, Any]]
    issues: list[dict[str, str]]
    parser: dict[str, Any]


class UpdateResult(TypedDict):
    status: str
    base_profile_version: int
    state: dict[str, Any]
    applied: list[str]
    ignored: list[dict[str, Any]]
    issues: list[dict[str, str]]
    changes: list[dict[str, Any]]


class LearningContext(TypedDict):
    client_id: str
    profile_version: int
    scope: dict[str, Any] | None
    team_declared: list[dict[str, Any]]
    tentative_preferences: list[dict[str, Any]]
    recent_attention: list[dict[str, Any]]
    explicit_constraints: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    unknowns: list[str]
    usage_notes: list[str]
    summary: str


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def issue(code, message):
    return {"code": code, "message": message}


def checked_id(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or value != value.strip():
        raise ValueError(f"{field} must be a nonempty ID (up to 128 characters).")
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("occurred_at must be an ISO datetime with a timezone.")
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("occurred_at must include a timezone.")
    return parsed.astimezone(timezone.utc)


def normalize_scope(value):
    if value is None or value == {}:
        return None
    if not isinstance(value, dict) or set(value) - {"account_id", "goal_id", "applicability", "analysis_id"}:
        raise ValueError("scope supports account_id, goal_id, applicability and analysis_id.")
    scope = {k: checked_id(v, k) for k, v in value.items() if v is not None}
    if scope.get("applicability", "ongoing") not in ("ongoing", "current"):
        raise ValueError("scope.applicability must be current or ongoing.")
    if scope.get("applicability") != "current":
        if "analysis_id" in scope:
            raise ValueError("analysis_id requires current applicability.")
        scope.pop("applicability", None)
    return scope or None


def event_scope(event):
    scope = event.get("scope")
    if scope and scope.get("applicability") == "current":
        return {**scope, "analysis_id": scope.get("analysis_id", event["evidence_group_id"])}
    return scope


def in_unit_interval(x):
    return type(x) in (float, int) and 0 <= x <= 1


def validate_event(raw):
    if not isinstance(raw, dict):
        raise ValueError("Event must be a JSON object.")
    event = json.loads(canonical(raw))
    if event.get("schema_version") != EVENT_SCHEMA:
        raise ValueError(f"schema_version must be {EVENT_SCHEMA}.")
    for name in ("event_id", "request_id", "client_id", "task", "entry_point"):
        checked_id(event.get(name), name)
    if type(event.get("revision")) is not int or event["revision"] < 1:
        raise ValueError("revision must be a positive integer.")
    timestamp(event.get("occurred_at"))
    event["evidence_group_id"] = checked_id(event.get("evidence_group_id") or event["event_id"],
                                             "evidence_group_id")
    if not isinstance(event.get("user_text", ""), str) or len(event.get("user_text", "")) > 12000:
        raise ValueError("user_text must be a string of at most 12000 characters.")
    event.setdefault("user_text", "")
    if event.get("outcome") not in OUTCOMES:
        raise ValueError("outcome must be received, success, error or cancelled.")
    event["scope"] = normalize_scope(event.get("scope"))
    for name in ("requested_parameters", "effective_parameters", "parameter_sources", "prior_context"):
        event[name] = event.get(name, {})
        if not isinstance(event[name], dict):
            raise ValueError(f"{name} must be an object.")
    for name in ("requested_parameters", "effective_parameters", "prior_context"):
        for field, value in event[name].items():
            if field in {"target_stock_weight", "current_stock_weight", "current_bond_weight",
                         "previous_scenario_stock_weight"} and value is not None and not in_unit_interval(value):
                raise ValueError(f"{name}.{field} must be between 0 and 1.")
    warnings = []
    for name, source in event["parameter_sources"].items():
        if not isinstance(source, str) or source not in SOURCES:
            event["parameter_sources"][name] = "unknown"
            warnings.append(issue("unknown_parameter_source", f"{name}: treated as unknown."))
    return event, warnings


def checked_rules(rules):
    if not isinstance(rules, dict):
        raise ValueError("Rules must be an object.")
    checked_id(rules.get("rule_version"), "rule_version")
    limit = rules.get("max_default_groups_per_scope")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("max_default_groups_per_scope must be a positive integer.")
    if rules.get("hypotheses") != {k: list(v) for k, v in DIMENSIONS.items()}:
        raise ValueError("Unexpected hypothesis order.")
    def weights(arr, n):
        return (isinstance(arr, list) and len(arr) == n and all(
            type(x) in (int, float) and 0 < x <= 1e100 for x in arr))
    for dim, labels in DIMENSIONS.items():
        prior = rules.get("priors", {}).get(dim)
        if not weights(prior, len(labels)) or not math.isclose(sum(prior), 1, abs_tol=1e-9):
            raise ValueError(f"Invalid prior for {dim}.")
    for key, rule in rules.get("rules", {}).items():
        dim = rule.get("dimension")
        if dim not in DIMENSIONS or not weights(rule.get("multipliers"), len(DIMENSIONS[dim])):
            raise ValueError(f"Invalid multipliers for {key}.")
    return rules
