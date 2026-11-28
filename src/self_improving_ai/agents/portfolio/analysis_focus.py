"""Translate the working willingness index into an additional, reproducible portfolio calculation."""
import math
from self_improving_ai.tools.portfolio.allocation import allocation_target, classified_values
from self_improving_ai.tools.portfolio.scenarios import PortfolioInputError


def previous_reference(card, note):
    """Rebase an existing manual adjustment once when upgrading the scoring policy."""
    automatic = card.get("score")
    note_score = note.get("score")
    note_legacy = any(row.get("legacy_text") for row in note.get("evidence", []))
    if note_score is not None and not note_legacy and card.get("status") != "conflict":
        return .85 * (automatic if automatic is not None else 50) + .15 * note_score
    return automatic


def evidence_estimate(rows, declaration, reset_at=None):
    """Only independently selected groups contribute. Exploration is bounded on both sides."""
    base = {"conservative": 25, "balanced": 50, "growth": 75}.get((declaration or {}).get("value"), 50)
    groups = {}
    for row in rows:
        if row.get("legacy_text") or (reset_at and row.get("occurred_at", "") < reset_at):
            continue
        rule = row.get("rule", "")
        direction = {"conservative": -1, "growth": 1}.get(row.get("value"), 0)
        if rule.endswith("_equity"):
            params = row.get("parameters") or {}
            target, current = params.get("target_stock_weight"), params.get("current_stock_weight")
            if not all(isinstance(v, (float, int)) and 0 <= v <= 1 for v in (target, current)):
                continue
            distance = abs(target - current)
            weight = (0.25 if rule.startswith("default_") else 2.0) * min(1, distance / .20)
            category = "trial"
        elif rule.startswith("hint_") and row.get("criterion") in {"directed_inquiry", "observed_risk_choice"}:
            weight = 4.0 if row["criterion"] == "observed_risk_choice" else 2.0
            category = "behavior"
        else:
            continue
        if direction:
            groups.setdefault(row["group"], []).append((category, direction * weight))
    totals = {"trial": 0.0, "behavior": 0.0}
    used = set()
    for group, votes in groups.items():
        if min(v for _, v in votes) * max(v for _, v in votes) < 0:
            continue
        category, vote = max(votes, key=lambda pair: abs(pair[1]))
        totals[category] += vote
        used.add(group)
    if declaration:
        used.add(declaration["group"])
    trial = 8 * math.tanh(totals["trial"] / 8)
    behavior = 12 * math.tanh(totals["behavior"] / 12)
    return {"score": base + trial + behavior, "anchor": base, "trial_points": trial,
            "behavior_points": behavior, "groups": len(used)}


def working_index(card, calibration, note=None, combined=None):
    note = note or {}
    combined = combined or card
    operations = card.get("evidence", [])
    # Notes document the client; an inferred advisor inquiry in this channel is not an observed action.
    notes = [r for r in note.get("evidence", []) if r.get("criterion") != "directed_inquiry"]
    hard_reasons = {"conflicting_statements", "declaration_denied"}
    if combined.get("scope"):
        hard_reasons.add("applicable_scopes_disagree")
    disputed = bool(hard_reasons.intersection(combined.get("conflicts", [])))
    def current_declaration(source):
        if hard_reasons.intersection(source.get("conflicts", [])):
            return None
        return next((r for r in reversed(source.get("evidence", [])) if r.get("kind") in
                     {"explicit_statement", "preference_change"} and not r.get("legacy_text")), None)
    declaration = current_declaration(combined)
    reset = declaration["occurred_at"] if declaration and declaration["kind"] == "preference_change" else None
    daily = evidence_estimate(operations, current_declaration(card), reset)
    estimate = evidence_estimate(operations + notes, declaration, reset)
    reference = estimate["score"]
    adjusted = calibration.get("chosen") is not None
    effective = reference
    if adjusted:
        delta = 0 if calibration.get("mode") == "fixed" else reference - calibration["baseline"]
        effective = min(100, max(0, calibration["chosen"] + delta))
    return {"automatic": daily["score"], "effective": effective, "reference": reference,
            "calibration": calibration, "groups": estimate["groups"], "breakdown": estimate,
            "anchor_source": declaration.get("source") if declaration else None,
            "mixed_trials": any("opposing_equal_evidence" in s.get("conflicts", []) for s in (card, note)),
            "note_legacy": any(r.get("legacy_text") for r in notes), "legacy_review": False,
            "status": "conflict" if disputed else "scored" if estimate["groups"] else "insufficient"}


def attach_scenarios(result, context, client_id, account_id=None):
    """Supplement the original tool result, preserving its target and holdings."""
    if result.get("status") != "success" or result.get("client_id") != client_id:
        return result
    context = context or {}
    index = context.get("scoped_risk_index") or context.get("working_risk_index")
    if not index:
        return result
    score = index["effective"]
    focus = "compare"
    step_pp = 5
    if score is not None and not index["legacy_review"] and (
            index["status"] != "conflict" or index["calibration"].get("chosen") is not None):
        step_pp = math.floor(5 + abs(score - 50) / 5 + .5)
        displayed_score = round(score, 1)
        if displayed_score < 45:
            focus = "downside"
        elif displayed_score > 55:
            focus = "upside"
    if result.get("asset_classes"):
        target = result["asset_classes"]["stock"]["target_weight"]
    elif result.get("scenario_comparison", {}).get("status") == "success":
        target = result["scenario_comparison"]["weights"]["stock"]
    else:
        target = None
    try:
        values = ({key: row["market_value"] for key, row in result["asset_classes"].items()}
                  if result.get("asset_classes") else classified_values(client_id, account_id or ""))
        baseline = allocation_target(client_id, target)["stock"]
    except (PortfolioInputError, ValueError, KeyError, OSError):
        return result
    total = sum(values.values())
    if total <= 0:
        return result
    # Fixed symmetric shock for comparability. Vary the extra allocation(s) examined.
    def scenario(label, weight):
        move = round(total * weight * .20, 2)
        difference = round(move - round(total * baseline * .20, 2), 2)
        return {"label": label, "stock_weight": weight, "stock_fall_usd": -move, "stock_rise_usd": move,
                "upside_change_usd": difference, "downside_change_usd": -difference}
    alternatives = []
    if focus in {"downside", "compare"} and baseline > 0:
        alternatives.append(scenario("less_equity", max(0, baseline - step_pp / 100)))
    if focus in {"upside", "compare"} and baseline < 1:
        alternatives.append(scenario("more_equity", min(1, baseline + step_pp / 100)))
    return {**result, "personalized_analysis": {
        "score_used": score, "focus": focus, "legacy_review": index["legacy_review"], "scope": index.get("scope"),
        "score_status": index["status"], "advisor_adjusted": index["calibration"].get("chosen") is not None,
        "stock_shock_pct": 20, "bond_shock_pct": 0, "scenario_step_pp": step_pp, "currency": "USD",
        "portfolio_value": total, "account_id": account_id or None,
        "target_source": ("request_override" if result.get("target_source") == "request_override" or
                          result.get("scenario_comparison", {}).get("source") == "manual_scenario" else "stored_draft"),
        "rows": [scenario("current", values["stock"] / total), scenario("baseline", baseline), *alternatives]}}
