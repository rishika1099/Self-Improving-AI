"""Build a read-only context without turning inferred preferences into tool parameters."""
from __future__ import annotations
import math
from .learner import evidence_multipliers, load_rules, normalize_log_support, risk_preference_index
from .schemas import DIMENSIONS, normalize_scope, timestamp


def _applies(candidate, requested):
    candidate, requested = candidate or {}, requested or {}
    if candidate.get("applicability") == "current" and not candidate.get("analysis_id"):
        return False
    return all(requested.get(key) == value for key, value in candidate.items())


def risk_score(view, state, rules, conflicts):
    belief = view["beliefs"]["risk_tilt"]
    ids = set(belief["active_signal_ids"])
    labels = DIMENSIONS["risk_tilt"]
    prior = rules["priors"]["risk_tilt"]
    logs = [math.log(p) for p in prior]
    ledger = []
    for signal in state["active_evidence"]:
        if signal["signal_id"] not in ids:
            continue
        factors = evidence_multipliers(signal, rules)
        logs = [v + math.log(factor) for v, factor in zip(logs, factors)]
        probs = dict(zip(labels, normalize_log_support(logs)))
        ledger.append({"event_id": signal["source_event_id"], "revision": signal["source_revision"],
                       "group": signal["evidence_group_id"], "rule": signal["rule_id"],
                       "source": signal.get("evidence_text"), "parameters": signal.get("basis"),
                       "criterion": signal.get("risk_basis"), "interpretation": signal.get("semantic_reason"),
                       "legacy_text": bool(signal.get("evidence_text") and not signal.get("risk_basis")),
                       "strength": signal["strength"], "multipliers": factors,
                       "running_score": risk_preference_index(probs)})
    reasons = [c["kind"] for c in conflicts if c.get("dimension") == "risk_tilt"
               and (c.get("scope") == view.get("scope") or c["kind"] == "applicable_scopes_disagree")]
    for row in ledger:
        # Sensitivity of the active set only. An actual retraction additionally replays
        # superseded evidence and can therefore have a different outcome.
        without = [v - math.log(m) for v, m in zip(logs, row["multipliers"])]
        row["score_without_this_factor"] = (risk_preference_index(dict(zip(labels, normalize_log_support(without))))
                                             if len(ledger) > 1 else None)
    status = "conflict" if reasons else "scored" if ledger else "insufficient"
    return {"score": risk_preference_index(belief["probabilities"]) if status == "scored" else None,
            "status": status, "scope": view.get("scope"), "support": belief["probabilities"],
            "prior": dict(zip(labels, prior)), "evidence": ledger, "conflicts": reasons,
            "rule_version": rules["rule_version"], "default_group_limit": rules.get("max_default_groups_per_scope")}


def build_context(state, scope=None, rules=None):
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
                belief = view["beliefs"][dim]
                ids = set(belief["active_signal_ids"])
                sources = [{"event_id": s["source_event_id"], "quote": s.get("evidence_text"),
                            "value": s["value"], "basis": s.get("basis", {})}
                           for s in state["active_evidence"] if s["signal_id"] in ids]
                tentative.append({"field":dim,"scope":view_scope, **belief, "sources":sources})
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
    cues = [s for s in recent if s["kind"] == "client_cue"]
    # Keep the quoted observation visible even when an explicit declaration anchors
    # the same dimension. A weaker hint may still prompt a clarifying question.
    cues.extend(s for s in state["active_evidence"]
                if s["kind"] == "behavioral_hint" and _applies(s["scope"], scope))
    cues.sort(key=lambda s: (timestamp(s["occurred_at"]), s["signal_id"]))
    recent = [s for s in recent if s["kind"] != "client_cue"]
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
    rules = rules or load_rules()
    scores = [risk_score(view, state, rules, conflicts) for view in applicable
              if view is state or view["beliefs"]["risk_tilt"]["active_signal_ids"]
              or any(c.get("dimension") == "risk_tilt" and c.get("scope") == view.get("scope") for c in conflicts)]
    return {"risk_preference_scores": scores, "client_id":state["client_id"],"profile_version":state["profile_version"],"scope":scope,
            "team_declared":declared,"tentative_preferences":tentative,"recent_attention":recent[-25:],
            "client_cues":cues[-25:], "explicit_constraints":constraints,"conflicts":conflicts,"unknowns":unknowns,
            "usage_notes":["Use context to explain portfolio analysis. Honor the advisor's current inputs.",
                           "An explored allocation does not imply adoption or execution.",
                           "Support scores are uncalibrated. Resolve contradictory or missing information with the advisor."],
            "summary":" ".join(summary)}
