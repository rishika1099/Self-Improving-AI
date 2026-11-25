"""Deterministic, replayable Bayesian-style support updates."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import json
import math

from .schemas import DIMENSIONS, STATE_SCHEMA, canonical, checked_rules, issue, timestamp

RANK = {"none": 0, "weak": 1, "medium": 2, "strong": 3, "explicit": 4}
STATEMENTS = {"explicit_statement", "preference_change"}


def load_rules(path=None):
    path = Path(path) if path else Path(__file__).with_name("signal_rules.json")
    return checked_rules(json.loads(path.read_text(encoding="utf-8")))


def _order(ex):
    event = ex["event"]
    return timestamp(event["occurred_at"]), event["event_id"], event["revision"]


def risk_preference_index(probabilities):
    """0, 50 and 100 are display coordinates for the three ordered hypotheses."""
    return 50 * probabilities["balanced"] + 100 * probabilities["growth"]


def normalize_log_support(logs):
    peak = max(logs)
    weights = [math.exp(x - peak) for x in logs]
    total = sum(weights)
    return [x / total for x in weights]


def _belief(dim, selected, rules, declared, conflicts):
    labels = DIMENSIONS[dim]
    priors = rules["priors"][dim]
    contributions = [evidence_multipliers(s, rules) for s in selected]
    if contributions:
        logs = [math.fsum([math.log(p)] + [math.log(c[i]) for c in contributions])
                for i, p in enumerate(priors)]
        probabilities = dict(zip(labels, normalize_log_support(logs)))
    else:
        probabilities = dict(zip(labels, priors))
    return {
        "probabilities": probabilities,
        "interpretation": "uncalibrated_prototype_support",
        "evidence_status": ("conflicting_evidence" if conflicts else
                            "explicit_statement_present" if declared else
                            "tentative" if selected else "unknown"),
        "active_signal_ids": [s["signal_id"] for s in selected],
    }


def evidence_multipliers(signal, rules):
    base = rules["rules"][signal["rule_id"]]["multipliers"]
    scale = signal.get("basis", {}).get("exposure_scale", 1)
    return [value ** scale for value in base]


def rebuild_state(initial_profile, evidence_history, rules):
    """Rebuild from stored extractions; optionally pass {'extractions': ..., 'retracted_event_ids': ...}."""
    history = evidence_history if isinstance(evidence_history, dict) else {"extractions": evidence_history}
    client = initial_profile["client_id"]
    extractions = [x for x in history.get("extractions", []) if x.get("event", {}).get("client_id") == client
                   and x.get("status") != "invalid"]
    events, ignored, conflicts, issues = {}, [], [], []
    revs = defaultdict(list)
    for ex in extractions:
        revs[ex["event"]["event_id"]].append(ex)

    def ignore(s, code, reason, winner=None):
        row = dict(signal_id=s["signal_id"], source_event_id=s["source_event_id"],
                   source_revision=s["source_revision"], reason_code=code, reason=reason)
        if winner:
            row["superseded_by"] = winner["signal_id"]
        ignored.append(row)

    retracted = set(history.get("retracted_event_ids", []))
    for eid, revisions in revs.items():
        revisions.sort(key=lambda e: e["event"]["revision"])
        selected = next((e for e in reversed(revisions) if e["status"] == "ok"
                         and (e["event"]["outcome"] not in {"error", "cancelled"}
                              or e["event"]["outcome"] == "error" and e["signals"])), revisions[-1])
        events[eid] = selected
        for ex in revisions:
            if ex is selected:
                continue
            for s in ex["signals"]:
                ignore(s, "inactive_revision", "Only the latest usable revision contributes evidence.")

    # Explicit correction entries are limited to observed prior events of the same client and scope.
    # Reverse chronology lets a later correction of an earlier correction restore the original.
    for ex in sorted(events.values(), key=_order, reverse=True):
        eid = ex["event"]["event_id"]
        if eid in retracted or ex["status"] != "ok":
            continue
        for s in ex["signals"]:
            if s["kind"] != "correction":
                continue
            target = events.get(s["value"])
            target_scopes = {canonical(v["scope"]) for v in target["signals"]} if target else set()
            if target and _order(target) < _order(ex) and target_scopes == {canonical(s["scope"])}:
                retracted.add(s["value"])
            else:
                ignore(s, "unresolved_correction", "Only prior evidence in the same client and scope can be retracted.")
                issues.append({**issue("needs_review", f"{eid}: correction target cannot be applied."),
                               "event_id": eid})

    candidates = defaultdict(list)
    retained, recent_attention, constraints = [], [], []
    for ex in sorted(events.values(), key=_order):
        for s in ex["signals"]:
            s = {**s, "occurred_at": ex["event"]["occurred_at"]}
            if ex["event"]["event_id"] in retracted:
                ignore(s, "retracted", "The original event is retained in history but does not contribute.")
                continue
            if s["dimension"] == "attention":
                retained.append(s)
                recent_attention.append(s)
            elif ex["status"] != "ok":
                ignore(s, "unresolved_event", "The event has unresolved semantics.")
            elif s["dimension"] == "constraint":
                constraints.append(s)
                retained.append(s)
            elif s["dimension"] in DIMENSIONS:
                rule = rules["rules"].get(s["rule_id"])
                if s["kind"] != "denial" and (not rule or rule["dimension"] != s["dimension"]):
                    ignore(s, "unknown_rule", "The rule has no applicable multiplier.")
                else:
                    candidates[(canonical(s["scope"]), s["dimension"])].append(s)
            elif s["kind"] == "correction" and s["value"] in retracted:
                retained.append(s)

    scopes = {"null": {"scope": None, "declared": {}, "beliefs": {}}}
    for (scope_key, dim), rows in candidates.items():
        scope_state = scopes.setdefault(scope_key, {"scope": json.loads(scope_key), "declared": {}, "beliefs": {}})
        resets = [s for s in rows if s["kind"] == "preference_change"]
        if resets:
            reset = resets[-1]
            pivot = rows.index(reset)
            for s in rows[:pivot]:
                ignore(s, "prior_preference_period", "A later explicit change starts a new preference period.", reset)
            rows = rows[pivot:]
        declarations = [s for s in rows if s["kind"] in STATEMENTS]
        active_declaration = declarations[-1] if declarations else None
        for s in declarations[:-1]:
            ignore(s, "superseded_statement", "Only the latest statement for this dimension and scope is current.",
                   active_declaration)
        denials = [s for s in rows if s["kind"] == "denial"]
        dim_conflicts = []
        if active_declaration:
            s = active_declaration
            scope_state["declared"][dim] = {"value":s["value"], "source_event_id":s["source_event_id"],
                                             "evidence_text":s["evidence_text"], "scope":s["scope"],
                                             "effective_from":s["occurred_at"]}
            competing = [d for d in denials if d["value"] == s["value"] and rows.index(d) > rows.index(s)]
            if competing:
                dim_conflicts.append({"dimension":dim,"scope":s["scope"],"kind":"declaration_denied",
                                      "evidence_ids":[s["signal_id"], competing[-1]["signal_id"]]})
            same_event = [d for d in declarations if d["source_event_id"] == s["source_event_id"]]
            if len({d["value"] for d in same_event}) > 1:
                dim_conflicts.append({"dimension":dim,"scope":s["scope"],"kind":"conflicting_statements",
                                      "evidence_ids":[d["signal_id"] for d in same_event]})
                ignore(s, "conflicting_statements", "One input gives conflicting statements for this scope.")
                scope_state["declared"].pop(dim)
                active_declaration = None
        groups = defaultdict(list)
        for s in rows:
            if s["kind"] == "denial" or (s["kind"] in STATEMENTS and s is not active_declaration):
                continue
            groups[s["evidence_group_id"]].append(s)
        selected = []
        for group in groups.values():
            max_rank = max(RANK[s["strength"]] for s in group)
            strongest = [s for s in group if RANK[s["strength"]] == max_rank]
            if len({s["value"] for s in strongest}) > 1:
                dim_conflicts.append({"dimension":dim, "scope":json.loads(scope_key),
                                      "kind":"opposing_equal_evidence",
                                      "evidence_ids":[s["signal_id"] for s in strongest]})
                for s in group:
                    ignore(s, "conflicting_group", "Equal opposing signals from one analysis are neutral.")
                continue
            winner = strongest[-1]
            selected.append(winner)
            for s in group:
                if s is not winner:
                    ignore(s, "superseded_within_group",
                           "A stronger or later signal from the same analysis represents this evidence.", winner)
        limit = rules.get("max_default_groups_per_scope")
        if limit is not None:
            defaults = sorted(
                (s for s in selected if s["rule_id"].startswith("default_")),
                key=lambda s: (timestamp(s["occurred_at"]), s["signal_id"]),
            )
            excess = {s["signal_id"] for s in defaults[:-limit]}
            for s in defaults[:-limit]:
                ignore(s, "default_evidence_limit", "Only recent independent default trials affect support.")
            selected = [s for s in selected if s["signal_id"] not in excess]
        if active_declaration:
            contrary = [s for s in selected if s["dimension"] == "risk_tilt"
                        and active_declaration["value"] in {"conservative", "growth"}
                        and s["value"] in {"conservative", "growth"}
                        and s["value"] != active_declaration["value"]]
            if contrary:
                dim_conflicts.append({"dimension":dim,"scope":active_declaration["scope"],
                                      "kind":"exploration_differs_from_declaration",
                                      "evidence_ids":[active_declaration["signal_id"], *[s["signal_id"] for s in contrary]]})
        retained.extend(selected + denials)
        conflicts.extend(dim_conflicts)
        scope_state["beliefs"][dim] = _belief(dim, selected, rules, active_declaration, dim_conflicts)

    for scope_state in scopes.values():
        for dim in DIMENSIONS:
            scope_state["beliefs"].setdefault(dim, _belief(dim, [], rules, None, []))
    general = scopes.pop("null")
    retained.sort(key=lambda s: (timestamp(s["occurred_at"]), s["signal_id"]))
    updated = max(extractions, key=_order)["event"]["occurred_at"] if extractions else None
    state = {"schema_version":STATE_SCHEMA, "rule_version":rules["rule_version"],
             "client_id":client, "profile_version":initial_profile.get("profile_version", 0),
             "declared":general["declared"], "beliefs":general["beliefs"],
             "scoped":[scopes[k] for k in sorted(scopes)],
             "attention":{"allocation_scenarios":[s for s in recent_attention if "target_stock_weight" in s],
                          "other":[s for s in recent_attention if "target_stock_weight" not in s]},
             "explicit_constraints":constraints, "active_evidence":retained, "conflicts":conflicts,
             "updated_at":updated}
    return {"state":state, "applied":[s["signal_id"] for s in retained], "ignored":ignored, "issues":issues}


def result_from(before, after, *, status="ok", issues=()):
    """Explain the actual changed fields and cite both removed and newly applicable evidence."""
    state = after["state"]
    changes = []
    risk_score_changes = []
    active = before.get("active_evidence", []) + state.get("active_evidence", [])

    def log(field, old, new, eligible, why):
        if old == new:
            return
        refs = list(dict.fromkeys(s["source_event_id"] for s in active if eligible(s)))
        changes.append({"change_type":"added" if old is None else "removed" if new is None else "updated",
                        "field":field, "before":deepcopy(old), "after":deepcopy(new),
                        "evidence_ids":refs,"reason":why})

    old_scopes = {canonical(r["scope"]):r for r in before.get("scoped", [])}
    new_scopes = {canonical(r["scope"]):r for r in state["scoped"]}
    old_scopes["null"], new_scopes["null"] = before, state
    for key in sorted(old_scopes.keys() | new_scopes.keys()):
        old, new = old_scopes.get(key, {}), new_scopes.get(key, {})
        prefix = "" if key == "null" else f"scoped[{key}]."
        for dim in DIMENSIONS:
            old_b = old.get("beliefs", {}).get(dim)
            new_b = new.get("beliefs", {}).get(dim)
            if dim == "risk_tilt" and old_b != new_b:
                def display(belief):
                    if belief and belief["active_signal_ids"] and belief["evidence_status"] != "conflicting_evidence":
                        return risk_preference_index(belief["probabilities"])
                    return None
                risk_score_changes.append({"scope": json.loads(key),
                                           "before": display(old_b), "after": display(new_b)})
            old_d = old.get("declared", {}).get(dim)
            new_d = new.get("declared", {}).get(dim)
            ids = (set(old_b["active_signal_ids"] if old_b else []) |
                   set(new_b["active_signal_ids"] if new_b else []))
            log(prefix+"declared."+dim, old_d, new_d,
                lambda s: s["source_event_id"] in {x["source_event_id"] for x in (old_d,new_d) if x},
                "Selected the current verified advisor statement for the scope.")
            log(prefix+f"beliefs.{dim}.probabilities", old_b["probabilities"] if old_b else None,
                new_b["probabilities"] if new_b else None, lambda s: s["signal_id"] in ids,
                "Recomputed support from the prior and applicable independent evidence groups.")
    for field, dim, reason in (
        ("attention","attention","Recorded or removed an observed topic or scenario."),
        ("explicit_constraints","constraint","Retained explicit constraints with their original evidence."),
        ("conflicts",None,"Reported conflicts in applicable evidence for review.")):
        if field != "conflicts":
            log(field, before.get(field), state.get(field), lambda s: s["dimension"] == dim, reason)
        else:
            ids = {eid for source in (before, state) for c in source.get(field, []) for eid in c["evidence_ids"]}
            log(field,before.get(field), state.get(field),lambda s: s["signal_id"] in ids,reason)
    return {"risk_score_changes":risk_score_changes, "status":status, "base_profile_version":before["profile_version"], "state":state,
            "applied":after["applied"], "ignored":after["ignored"],
            "issues":[*issues,*after["issues"]], "changes":changes}


def update_state(state, incoming, rules, evidence_history=()):
    """Pure update. The caller retains extractions and persists the returned state."""
    history = deepcopy(evidence_history if isinstance(evidence_history, dict) else {"extractions":list(evidence_history)})
    event = incoming.get("event", {})
    if event.get("client_id") != state["client_id"] or incoming["status"] == "invalid":
        return {"status":"invalid", "base_profile_version":state["profile_version"], "state":deepcopy(state),
                "applied":[], "ignored":[], "issues":incoming.get("issues") or [issue("client_mismatch","Wrong client.")],
                "changes":[]}
    records = history.setdefault("extractions", [])
    existing = next((x for x in records if all(x["event"].get(k) == event[k]
                    for k in ("client_id","event_id","revision"))), None)
    if existing:
        same = existing["event"] == event
        return {"status":"already_processed" if same else "conflict", "base_profile_version":state["profile_version"],
                "state":deepcopy(state), "applied":[], "ignored":[],
                "issues":[issue("duplicate" if same else "revision_conflict",
                                "Same input already processed." if same else "Revision content differs.")], "changes":[]}
    records.append(incoming)
    after = rebuild_state({**state,"profile_version":state["profile_version"]+1}, history, rules)
    result = result_from(state, after, status=incoming["status"], issues=incoming["issues"])
    if result["status"] == "ok" and any(
        i["code"] == "needs_review" and i.get("event_id") == event["event_id"]
        for i in result["issues"]
    ):
        result["status"] = "needs_review"
    return result
