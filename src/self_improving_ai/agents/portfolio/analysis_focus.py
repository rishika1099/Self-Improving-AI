"""Translate the working willingness index into an additional, reproducible portfolio calculation."""
import math
from self_improving_ai.tools.portfolio.allocation import allocation_target, classified_values
from self_improving_ai.tools.portfolio.scenarios import PortfolioInputError


def working_index(card, calibration, note=None):
    automatic = card.get("score")
    note = note or {}
    note_score = note.get("score")
    note_legacy = any(row.get("legacy_text") for row in note.get("evidence", []))
    reference = automatic
    if note_score is not None and not note_legacy and card.get("status") != "conflict":
        reference = .85 * (automatic if automatic is not None else 50) + .15 * note_score
    adjusted = calibration.get("chosen") is not None
    effective = reference
    if adjusted:
        delta = reference - calibration["baseline"] if reference is not None else 0
        effective = min(100, max(0, calibration["chosen"] + delta))
    legacy = any(row.get("legacy_text") for row in card.get("evidence", []))
    return {"automatic": automatic, "effective": effective, "reference":reference, "note_score":note_score,
            "note_legacy":note_legacy, "note_status":note.get("status"), "calibration": calibration,
            "status": card.get("status"), "groups": len(card.get("evidence", [])),
            "legacy_review": legacy and not adjusted}


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
    if score is not None and not index["legacy_review"]:
        step_pp = math.floor(5 + abs(score - 50) / 5 + .5)
        if score < 45:
            focus = "downside"
        elif score > 55:
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
        "target_source": ("request_override" if result.get("target_source") == "request_override" or
                          result.get("scenario_comparison", {}).get("source") == "manual_scenario" else "stored_draft"),
        "rows": [scenario("current", values["stock"] / total), scenario("baseline", baseline), *alternatives]}}
