"""Convert validated events and exact text excerpts to versioned signals."""
from __future__ import annotations
import json
from .parser import ParseError, ParsePacket, numbered_excerpts
from .schemas import DIMENSIONS, canonical, event_scope, issue, normalize_scope, validate_event

CUE_TOPICS = {"liquidity", "income_goal", "capital_preservation", "leverage", "risk_change", "adjustment_pace", "other"}
TASK_TOPICS = {"risk": "risk", "holdings": "holdings", "projection": "projection",
               "quote": "quote", "history": "history"}

KINDS = {"behavioral_hint", "client_cue", "explicit_statement", "reasoned_choice", "attention", "constraint", "correction", "preference_change"}
ASSERTIONS = {"affirmed", "hypothetical", "negated", "uncertain"}


def extract_signals(event, parse_text=None):
    try:
        event, issues = validate_event(event)
    except (ValueError, TypeError, OverflowError) as exc:
        return {"event": {}, "status": "invalid", "signals": [],
                "issues": [issue("invalid_event", str(exc))], "parser": {"mode": "not_called"}}
    signals, counters = [], {}
    default_scope = event_scope(event)

    def make(tag, dimension, kind, strength, rule, value, reason, **kwargs):
        counters[tag] = counters.get(tag, 0) + 1
        suffix = "" if counters[tag] == 1 else f"-{counters[tag]}"
        signal = dict(signal_id=f"{event['event_id']}-{tag}{suffix}",
                      source_event_id=event["event_id"], source_revision=event["revision"],
                      evidence_group_id=event["evidence_group_id"], dimension=dimension,
                      kind=kind, rule_id=rule, strength=strength, value=value,
                      scope=kwargs.pop("scope", default_scope), reason=reason, **kwargs)
        signals.append(signal)

    parser = {"mode": "not_called"}
    status, items = "ok", []
    failed = event["outcome"] in {"error", "cancelled"}
    if failed and not (
        event["outcome"] == "error" and event["entry_point"] in {"agent", "advisor_note"}
        and event["user_text"].strip()
    ):
        return {"event": event, "status": "ok", "signals": [], "issues": issues, "parser": parser}
    if event["user_text"].strip():
        if parse_text is None:
            status = "pending"
            issues.append(issue("parser_missing", "Configure a parser and submit a higher revision."))
        else:
            try:
                scoped_parser = getattr(type(parse_text), "parse_scoped", None)
                packet = (scoped_parser(parse_text, event["user_text"], default_scope)
                          if scoped_parser else parse_text(event["user_text"]))
                if not isinstance(packet, ParsePacket):
                    packet = ParsePacket(packet if isinstance(packet, str) else canonical(packet), "injected")
                parser = {"mode": packet.mode, "model": packet.model, "prompt_version": packet.prompt_version,
                          "raw": packet.raw}
                if not isinstance(packet.raw, str) or len(packet.raw) > 32000:
                    parser.pop("raw", None)
                    raise ValueError("Parser result is too large or not text.")
                result = json.loads(packet.raw)
                if isinstance(result, dict) and set(result) == {"signals"}:
                    result = result["signals"]
                if not isinstance(result, list) or len(result) > 20:
                    raise ValueError("Parser must return a list of up to 20 semantic signals.")
                items = [_validate_text(item, event) for item in result]
            except ParseError as exc:
                status = "needs_review" if exc.code == "scope_unresolved" else "pending"
                detail = issue(exc.code, str(exc))
                if exc.retry_at:
                    detail["retry_at"] = exc.retry_at
                issues.append(detail)
            except TimeoutError:
                status = "pending"
                issues.append(issue("model_timeout", "Text parsing timed out."))
            except (ValueError, TypeError, KeyError) as exc:
                status = "partial"
                issues.append(issue("invalid_parse", str(exc)))
            except Exception:
                status = "pending"
                issues.append(issue("parser_failure", "Parser did not return a usable response."))
        parser["validation"] = status

    effective = event["effective_parameters"]
    requested = event["requested_parameters"]
    target = effective.get("target_stock_weight")
    if target is None:
        target = requested.get("target_stock_weight")
    source = event["parameter_sources"].get("target_stock_weight", "unknown")
    if not failed and event["task"] in {"allocation", "allocation_drift"} and target is not None:
        make("ATTENTION", "attention", "query_interest", "none", "attention_only", target,
             "An allocation scenario was explored; adoption is not established.",
             target_stock_weight=target, parameter_source=source, adoption_status="not_established",
             basis={"target_stock_weight": target, "current_stock_weight":event["prior_context"].get("current_stock_weight"),
                    "previous_scenario_stock_weight":event["prior_context"].get("previous_scenario_stock_weight")})
        current = event["prior_context"].get("current_stock_weight")
        suppress = status != "ok" or any(i["assertion"] != "affirmed" or i["kind"] in
                   {"attention", "correction"} for i in items)
        consistent = (source == "default" and "target_stock_weight" not in requested) or (
            source == "manual" and requested.get("target_stock_weight") == target)
        if (not suppress and consistent and event["outcome"] == "success"
            and effective.get("target_stock_weight") is not None and current is not None
            and abs(target - current) > 1e-12):
            direction = "lower" if target < current else "raise"
            make("RISK", "risk_tilt", "allocation_exploration", "weak" if source == "default" else "medium",
                 f"{source}_{direction}_equity", "conservative" if direction == "lower" else "growth",
                 "Compared the explored target to current equity exposure, not to an approved allocation.",
                 basis={"current_stock_weight":current, "target_stock_weight":target,
                        "previous_scenario_stock_weight":event["prior_context"].get("previous_scenario_stock_weight"),
                        "parameter_source":source})
    if event["entry_point"] == "direct" and event["outcome"] == "success" and event["task"] in TASK_TOPICS:
        task = event["task"]
        basis = {"task": task, **event.get("result_summary", {})}
        if task == "projection":
            basis["manual_parameters"] = {k: effective[k] for k in effective
                                          if event["parameter_sources"].get(k) == "manual"}
        if task in {"quote", "history"}:
            basis["symbol"] = effective.get("symbol")
        make("TOPIC", "attention", "calculation_topic", "none", "attention_only", task,
             "Completed calculation; retain the topic and observed result for follow-up.", basis=basis)
    for key in (() if failed else ("symbol", "ticker", "metric")):
        value = requested.get(key, effective.get(key))
        if isinstance(value, str) and value.strip():
            make("ATTENTION", "attention", "query_interest", "none", "attention_only", value,
                 f"Request concerned {key}; no purchase intent or risk preference inferred.",
                 basis={key:value}, adoption_status="not_established")

    for item in items:
        kind, dim, value, assertion = (item[k] for k in ("kind","dimension","value","assertion"))
        scope, evidence = item["scope"], item["evidence_text"]
        extra = dict(scope=scope, evidence_text=evidence, assertion=assertion)
        if kind == "correction":
            target_id = item.get("target_event_id")
            if target_id and assertion == "affirmed":
                make("CORRECTION", "correction", kind, "none", "retract_event", target_id,
                     "Advisor explicitly identified an event to retract.", **extra)
            else:
                issues.append(issue("needs_review", "Correction target is missing, unconfirmed or ambiguous."))
                make("CORRECTION", "attention", "unresolved_correction", "none", "attention_only",
                     evidence, "Preserved the correction without guessing its target.", **extra)
        elif assertion != "affirmed" or kind == "attention":
            make("ATTENTION", "attention", assertion if assertion != "affirmed" else "query_interest",
                 "none", "attention_only", value, "Kept the original context without changing the belief.", **extra)
            # A direct denial conflicts with a matching active declaration; it does not imply its opposite.
            if kind == "explicit_statement" and assertion == "negated":
                make("DENIAL", dim, "denial", "none", "attention_only", value,
                     "An explicit denial needs reconciliation with matching declarations.", **extra)
        elif kind == "behavioral_hint":
            make("HINT", dim, kind, "weak", "hint_" + value, value, item["reason"], **extra)
        elif kind == "client_cue":
            make("CUE", "attention", kind, "weak", "attention_only", value, item["reason"], **extra)
        elif kind == "constraint":
            make("CONSTRAINT", dim, kind, "explicit", "explicit_constraint", value,
                 "Recorded the quoted constraint without inferring a numerical limit.", **extra)
        else:
            tag = "RISK" if dim == "risk_tilt" else "ADJUSTMENT"
            strength = "strong" if kind == "reasoned_choice" else "explicit"
            make(tag, dim, kind, strength, ("choice_" if strength == "strong" else "explicit_") + value,
                 value, "Applied the verified excerpt as a scoped advisor signal.", **extra)
    if status == "ok" and any(x["code"] == "needs_review" for x in issues):
        status = "needs_review"
    return {"event": event, "status": status, "signals": signals, "issues": issues, "parser": parser}


def _validate_text(raw, event):
    if not isinstance(raw, dict):
        raise ValueError("Each semantic signal must be an object.")
    i = {k: raw.get(k) for k in ("kind","dimension","value","assertion","scope","evidence_text","reason","target_event_id")}
    kind, dim, val, assertion, evidence = (i[k] for k in ("kind","dimension","value","assertion","evidence_text"))
    if not all(isinstance(x, str) for x in (kind, dim, assertion)) or kind not in KINDS or assertion not in ASSERTIONS:
        raise ValueError("Unknown semantic kind, dimension or assertion.")
    if "evidence_id" in raw:
        sentences = numbered_excerpts(event["user_text"])
        ref = raw["evidence_id"]
        if type(ref) is not int or not 1 <= ref <= len(sentences):
            raise ValueError("Evidence must refer to a supplied sentence.")
        evidence = i["evidence_text"] = sentences[ref - 1]
    if not isinstance(evidence, str) or not evidence.strip() or evidence not in event["user_text"]:
        raise ValueError("Every semantic signal needs an exact excerpt from the original request.")
    if not isinstance(i["reason"], str) or not i["reason"].strip():
        raise ValueError("Every semantic signal needs a reason.")
    raw_scope = i["scope"]
    if isinstance(raw_scope, dict):
        unresolved = raw_scope.get("unresolved", False)
        if type(unresolved) is not bool:
            raise ValueError("scope.unresolved must be a boolean.")
        if unresolved:
            raise ParseError("scope_unresolved", "Bind the named account or goal before applying this input.")
        raw_scope = {k: v for k, v in raw_scope.items() if k != "unresolved"}
        # Normalize the observed legacy Gemini alias without changing identity checks.
        if raw_scope.get("applicability") == "this_analysis_only":
            raw_scope["applicability"] = "current"
        if raw_scope.get("applicability") == "long_term":
            raw_scope["applicability"] = "ongoing"
    scope = normalize_scope(raw_scope)
    source_scope = event_scope(event) or {}
    if scope:
        for k in ("account_id", "goal_id", "analysis_id"):
            if k in scope and scope[k] != source_scope.get(k):
                raise ValueError("The parser cannot create or change an account, goal or analysis ID.")
    merged = {**source_scope, **(scope or {})}
    if kind == "reasoned_choice" or merged.get("applicability") == "current":
        merged.update(applicability="current", analysis_id=source_scope.get("analysis_id", event["evidence_group_id"]))
    i["scope"] = merged or None
    if kind in {"behavioral_hint", "explicit_statement", "preference_change", "reasoned_choice"}:
        if dim not in DIMENSIONS or val not in DIMENSIONS[dim]:
            raise ValueError("Unknown preference dimension or value.")
        if kind == "reasoned_choice" and dim != "adjustment_style":
            raise ValueError("v0 only scores reasoned choices for adjustment style.")
        if kind == "preference_change" and merged.get("applicability") == "current":
            raise ValueError("A current-session choice cannot reset an ongoing preference.")
    elif kind == "constraint":
        if dim != "constraint" or (val is not None and not isinstance(val, str)):
            raise ValueError("Invalid constraint field.")
        # The literal instruction is the constraint. A novel topic can safely retain that
        # instruction as 'other', without populating any inferred numeric or named limit.
        if val not in {"max_loss_limit", "budget", "restricted_funds", "leverage", "other"}:
            i["value"] = "other"
    elif kind == "client_cue":
        if dim != "attention" or not isinstance(val, str) or not val:
            raise ValueError("Invalid client observation topic.")
        if val not in CUE_TOPICS:
            i["value"] = "other"
    elif kind == "attention":
        if dim != "attention":
            raise ValueError("Attention dimensions must match the kind.")
        i["value"] = evidence
    elif kind == "correction":
        if dim != "correction":
            raise ValueError("Correction dimensions must match the kind.")
        target = i["target_event_id"]
        if not isinstance(target, str) or not target or target not in evidence.split():
            i["target_event_id"] = None
    return i
