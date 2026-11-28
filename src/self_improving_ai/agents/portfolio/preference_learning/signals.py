"""Convert validated events and exact text excerpts to versioned signals."""
from __future__ import annotations
import json
import re
import unicodedata
from .prompts import CLAUSE_INDEX_VERSIONS, PROMPT_VERSION
from .parser import ParseError, ParsePacket, RISK_BASES, numbered_excerpts
from .schemas import DIMENSIONS, canonical, event_scope, issue, normalize_scope, validate_event

CUE_TOPICS = {"liquidity", "income_goal", "return_priority", "loss_reaction", "capital_preservation",
              "leverage", "risk_change", "adjustment_pace", "other"}
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
                rejected = []
                for raw_item in result:
                    try:
                        items.append(_validate_text(raw_item, event, parser.get("prompt_version")))
                    except (ValueError, TypeError, KeyError, ParseError) as exc:
                        rejected.append(exc)
                if rejected and not items:
                    raise rejected[0]
                if rejected:
                    issues.append(issue("incomplete_coverage", "Some interpretations could not be bound to the source. Review the original request."))
                if parser.get("prompt_version") in CLAUSE_INDEX_VERSIONS:
                    spans = numbered_excerpts(event["user_text"], independent_clauses=True)
                    covered = {item["evidence_text"] for item in items}
                    if len(spans) > 1:
                        for span in spans:
                            if span not in covered:
                                make("REVIEW", "attention", "uninterpreted_excerpt", "none", "attention_only",
                                     span, "No interpretation returned for this segment.", evidence_text=span)
                                issues.append(issue("incomplete_coverage", "An input segment was saved for review."))
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
    allocation_trial = event["task"] in {"allocation", "allocation_drift"} or (
        event["task"] == "risk" and source == "manual" and "target_stock_weight" in requested)
    if not failed and allocation_trial and target is not None:
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
                        "parameter_source":source,
                        "exposure_scale":exposure_scale(event, current, target) if source == "manual" else 1})
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

    retained_topics = {(i.get("value"), i["evidence_text"], canonical(i.get("scope"))) for i in items
                       if i["kind"] == "client_cue" and i["assertion"] == "affirmed"}
    for item in items:
        kind, dim, value, assertion = (item[k] for k in ("kind","dimension","value","assertion"))
        scope, evidence = item["scope"], item["evidence_text"]
        extra = dict(scope=scope, evidence_text=evidence, assertion=assertion)
        if event["entry_point"] == "agent" and event.get("text_source") == "default":
            make("ATTENTION", "attention", "query_interest", "none", "attention_only", evidence,
                 "System-supplied question.", **extra)
            continue
        topic = item.get("client_topic")
        topic_key = (topic, evidence, canonical(scope))
        if (assertion == "affirmed" and topic in CUE_TOPICS - {"other"} and topic_key not in retained_topics):
            retained_topics.add(topic_key)
            make("CUE", "attention", "client_cue", "weak", "attention_only", topic, item["reason"], **extra)
        inquiry_direction = {"lower":"conservative","higher":"growth","balanced":"balanced"}.get(item.get("risk_direction"))
        if (item.get("risk_basis") == "directed_inquiry" and assertion == "affirmed"
                and event["entry_point"] == "agent" and dim == "attention" and inquiry_direction
                and topic in {"other","risk_change"}):
            make("HINT","risk_tilt","behavioral_hint","weak","hint_"+inquiry_direction,inquiry_direction,
                 item["reason"],**extra,risk_basis="directed_inquiry",semantic_reason=item["reason"])
        if (item.get("risk_basis") == "stock_target_inquiry" and kind == "attention" and
                assertion == "affirmed" and event["entry_point"] == "agent" and event["outcome"] == "success"):
            trial = verified_chat_target(event, item)
            if trial:
                current, target = trial
                lower = target < current
                make("TRIAL", "risk_tilt", "allocation_exploration", "medium",
                     "manual_lower_equity" if lower else "manual_raise_equity",
                     "conservative" if lower else "growth", item["reason"],
                     **{**extra, "scope": verified_trial_scope(event, item)},
                     risk_basis="stock_target_inquiry", semantic_reason=item["reason"],
                     basis={"current_stock_weight": current, "target_stock_weight": target,
                            "parameter_source": "manual", "exposure_scale": exposure_scale(event, current, target)})
            else:
                make("REVIEW", "attention", "unverified_target", "none", "attention_only", evidence,
                     "The request and the executed stock target do not identify a single verified choice.", **extra)
        if dim == "risk_tilt":
            basis = item.get("risk_basis")
            extra.update(risk_basis=basis, risk_intensity=item.get("risk_intensity"), semantic_reason=item["reason"],
                         prompt_version=parser.get("prompt_version"))
            if basis == "directed_inquiry" and (event["entry_point"] == "advisor_note" or topic in {"liquidity", "income_goal"}):
                make("ATTENTION","attention","unscored_observation","none","attention_only",evidence,
                     "A goal or note alone does not verify a directional portfolio inquiry.", **extra)
                continue
            if (basis == "directed_inquiry" and assertion == "affirmed" and item.get("risk_direction") is not None
                    and inquiry_direction != value):
                make("REVIEW","attention","uninterpreted_excerpt","none","attention_only",evidence,
                     "Direction fields disagree.",**extra)
                issues.append(issue("incomplete_coverage","Risk direction requires review."))
                continue
            if (parser.get("prompt_version") == PROMPT_VERSION or basis is not None) and assertion == "affirmed":
                if basis in {"observed_risk_choice", "directed_inquiry", "allocation_intent", "current_risk_attitude"}:
                    kind = "behavioral_hint"
                elif basis not in {"declared_attitude", "risk_return_tradeoff"}:
                    make("ATTENTION", "attention", "unscored_observation", "none", "attention_only",
                         evidence, "No supported direction for risk willingness.", **extra)
                    continue
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


def exposure_scale(event, current, target):
    if event.get("operation_scoring") == "equity-distance-v1":
        return min(1.5, abs(target - current) / .20)
    return 1


def mentioned_percentages(excerpt):
    text = unicodedata.normalize("NFKC", excerpt).lower()
    number = r"(?<![\d.])(\d+(?:\.\d+)?)"
    amounts = {float(n) for n in re.findall(number + r"\s*(?:%|per\s*cent\b|percent\b)", text)}
    for ratio in re.finditer(number + r"\s*[/：:]\s*(\d+(?:\.\d+)?)", text):
        a, b = map(float, ratio.groups())
        if abs(a+b-100) < 1e-9:
            prefix = text[max(0, ratio.start()-28):ratio.start()]
            suffix = text[ratio.end():ratio.end()+20]
            bonds_first = re.search(r"(?:债券?\s*[/比与和]?\s*股|bonds?\s*[/:-]\s*(?:stocks?|equity))", prefix+" "+suffix)
            amounts.add(b if bonds_first else a)
    digits = {ch: n for n, ch in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    for whole, tenths in re.findall(r"([一二两三四五六七八九十0-9])成([一二三四五六七八九0-9]?)", text):
        numeral = lambda c: int(c) if c.isascii() else digits.get(c, 10)
        amounts.add(10*numeral(whole) + (numeral(tenths) if tenths else 0))
    for cn in re.findall(r"百分之([零一二两三四五六七八九十百]+)", text):
        if cn == "一百":
            amounts.add(100)
        elif "十" in cn:
            tens, _, ones = cn.partition("十")
            if tens in ("", *digits) and ones in ("", *digits):
                amounts.add(10*digits.get(tens,1) + digits.get(ones,0))
        elif cn in digits:
            amounts.add(digits[cn])
    return amounts


def stock_targets_in(text):
    """Bind labeled percentages to their asset before considering the executed target."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    clauses = re.split(r"[,;。；、!?！？\n]|\b(?:and|or|versus|vs\.?)\b|和|以及|还是|或者", normalized)
    targets = set()
    for clause in clauses:
        transition = re.search(r"(?:从|\bfrom\b).+?(?:调(?:整)?(?:至|到)|改(?:为|到)|降(?:至|到)|升(?:至|到)|\bto\b)", clause)
        if transition:
            clause = clause[transition.end():]
        elif (re.search(r"目前|当前|现有|\bcurrent(?:ly)?\b", clause)
              and not re.search(r"调|目标|改|试算|\b(?:target|to|test)\b", clause)):
            continue
        amounts = mentioned_percentages(clause)
        if not amounts:
            continue
        if re.search(r"\d\s*[/：:]\s*\d", clause):
            targets.update(amounts)  # The ratio parser already handles bonds-first labels.
            continue
        stocks = re.search(r"股票|权益|股权|\bstocks?\b|\bequit(?:y|ies)\b", clause)
        bonds = re.search(r"债券|固收|\bbonds?\b", clause)
        if stocks and bonds:
            return set()  # Multiple assets with no unique amount-to-asset binding.
        if not stocks and not bonds and re.search(r"收益|回撤|波动|费用|税率|return|drawdown|volatility|fee", clause):
            continue
        targets.update(round(100 - p, 9) if bonds else p for p in amounts)
    return targets


def verified_trial_scope(event, item):
    scope = dict(item.get("scope") or {})
    if (event_scope(event) or {}).get("applicability") == "current":
        return scope
    text = unicodedata.normalize("NFKC", event["user_text"]).lower()
    if re.search(r"演练|临时|仅.{0,4}(?:本次|本轮|这次)|只.{0,4}(?:本次|本轮|这次)|"
                 r"不要.{0,6}(?:记录|记入|长期)|(?:这次|本次|本轮).{0,5}(?:而已|专用|有效)|"
                 r"\b(?:one.off|hypothetical|only (?:for )?this (?:analysis|round|time)|this (?:analysis|round|time) only)\b", text):
        return {**scope, "applicability":"current", "analysis_id":event["evidence_group_id"]}
    if item.get("scope_quote"):
        return {**scope, "applicability":"current", "analysis_id":event["evidence_group_id"]}
    if scope.get("applicability") == "current" and item.get("scope_quote") == "":
        # A verified operation describes exploration. A temporary modifier must be
        # supported separately. Missing fields from older parsers retain their scope.
        scope.pop("applicability", None)
        scope.pop("analysis_id", None)
    return scope or None


def verified_chat_target(event, item):
    percent = item.get("stock_target_pct")
    if type(percent) not in (float, int) or not 0 <= percent <= 100:
        return None
    if stock_targets_in(item["evidence_text"]) != {percent}:
        return None
    original = event.get("user_text", item["evidence_text"])
    if len(stock_targets_in(original)) > 1:
        return None
    target = percent / 100
    matches = set()
    for row in event.get("execution_trace", []):
        if row.get("kind") != "result" or row.get("tool") not in {"calculate_allocation_drift", "estimate_portfolio_risk"}:
            continue
        result = row.get("result") or {}
        if (result.get("status") != "success" or result.get("client_id") != event["client_id"]
                or (result.get("account_id") or None) != (item.get("scope") or {}).get("account_id")):
            continue
        stock = result.get("asset_classes", {}).get("stock", {})
        comp = result.get("scenario_comparison", {})
        if stock and result.get("target_source") == "request_override":
            current, executed = stock.get("current_weight"), stock.get("target_weight")
        elif comp.get("source") == "manual_scenario":
            current, executed = result.get("weights", {}).get("stock"), comp.get("weights", {}).get("stock")
        else:
            continue
        if type(current) in (int,float) and type(executed) in (int,float):
            matches.add((current, executed))
    if len(matches) == 1:
        current, executed = matches.pop()
        if abs(executed-target) < 1e-9 and abs(current-target) > 1e-9:
            return current, target
    return None


def _validate_text(raw, event, prompt_version=None):
    if not isinstance(raw, dict):
        raise ValueError("Each semantic signal must be an object.")
    i = {k: raw.get(k) for k in ("kind","dimension","value","assertion","scope","evidence_text","reason","target_event_id","risk_basis","stock_target_pct","client_topic","risk_direction","scope_quote","risk_intensity")}
    if i["risk_intensity"] not in (None, "mild", "moderate", "strong"):
        raise ValueError("Invalid intensity for a risk declaration.")
    if prompt_version == PROMPT_VERSION and i["risk_basis"] not in (None, *RISK_BASES):
        raise ValueError("Unsupported basis for a risk signal.")
    if i["scope_quote"] is not None and (not isinstance(i["scope_quote"], str) or i["scope_quote"] not in event["user_text"]):
        raise ValueError("Scope restrictions must quote the original request.")
    kind, dim, val, assertion, evidence = (i[k] for k in ("kind","dimension","value","assertion","evidence_text"))
    if any(i[key] is not None and not isinstance(i[key], str) for key in ("risk_basis","client_topic","risk_direction")):
        raise ValueError("The risk basis and client topic must be text.")
    if not all(isinstance(x, str) for x in (kind, dim, assertion)) or kind not in KINDS or assertion not in ASSERTIONS:
        raise ValueError("Unknown semantic kind, dimension or assertion.")
    if "evidence_id" in raw:
        sentences = numbered_excerpts(event["user_text"], independent_clauses=prompt_version in CLAUSE_INDEX_VERSIONS)
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
