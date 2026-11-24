"""Build a read-only context without turning inferred preferences into tool parameters."""
from __future__ import annotations
from .schemas import DIMENSIONS, normalize_scope, timestamp


def _applies(candidate, requested):
    candidate, requested = candidate or {}, requested or {}
    if candidate.get("applicability") == "current" and not candidate.get("analysis_id"):
        return False
    return all(requested.get(key) == value for key, value in candidate.items())


def build_context(state, scope=None):
    scope = normalize_scope(scope)
    applicable = [state] + [r for r in state["scoped"] if _applies(r["scope"], scope)]
    declared, tentative = [], []
    conflicts = [c for c in state["conflicts"] if _applies(c["scope"], scope)]
    constraints = [s for s in state["explicit_constraints"] if _applies(s["scope"], scope)]
    for dim in DIMENSIONS:
        specific = []
        for view in applicable:
            s = view["declared"].get(dim)
            view_scope = view.get("scope")
            if s:
                declared.append({"field":dim, **s})
                specific.append(s)
            elif view["beliefs"][dim]["active_signal_ids"]:
                tentative.append({"field":dim,"scope":view_scope, **view["beliefs"][dim]})
        if len({s["value"] for s in specific}) > 1:
            conflicts.append({"dimension":dim,"kind":"applicable_scopes_disagree",
                              "evidence_ids":[s["source_event_id"] for s in specific]})
    for field in sorted({s["value"] for s in constraints} - {"other"}):
        rows = [s for s in constraints if s["value"] == field]
        if len({r["evidence_text"] for r in rows}) > 1:
            conflicts.append({"dimension":"constraint","kind":"review_multiple_constraints",
                              "field":field,"evidence_ids":[s["signal_id"] for s in rows]})
    recent = [s for seq in state["attention"].values() for s in seq if _applies(s["scope"], scope)]
    recent.sort(key=lambda s:(timestamp(s["occurred_at"]),s["signal_id"]))
    unknowns = [dim for dim in DIMENSIONS if not any(d["field"] == dim for d in declared + tentative)]
    if not any(s["value"] == "max_loss_limit" for s in constraints):
        unknowns.append("maximum_loss_limit")
    unknowns.append("adopted_target_allocation")
    statements = [f"{s['field']}: {s['value']}" for s in declared]
    trials = [f"{s['target_stock_weight']:.0%} equity" for s in recent if "target_stock_weight" in s]
    summary = []
    if statements:
        summary.append("Advisor statements: "+", ".join(statements)+".")
    if tentative:
        summary.append("Tentative evidence: "+", ".join(dict.fromkeys(s["field"] for s in tentative))+".")
    if trials:
        summary.append("Explored "+", ".join(dict.fromkeys(trials))+"; adoption is not established.")
    if conflicts:
        summary.append("Some applicable evidence conflicts; verify with the advisor.")
    summary.append("Unknown: "+", ".join(unknowns)+".")
    return {"client_id":state["client_id"],"profile_version":state["profile_version"],"scope":scope,
            "team_declared":declared,"tentative_preferences":tentative,"recent_attention":recent[-25:],
            "explicit_constraints":constraints,"conflicts":conflicts,"unknowns":unknowns,
            "usage_notes":["Use context to explain portfolio analysis. Honor the advisor's current inputs.",
                           "An explored allocation does not imply adoption or execution.",
                           "Support scores are uncalibrated. Resolve contradictory or missing information with the advisor."],
            "summary":" ".join(summary)}
