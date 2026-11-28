"""Replay the inputs that drive the displayed willingness score."""
from collections import defaultdict
from datetime import datetime
import math
import re
import unicodedata
from zoneinfo import ZoneInfo

from self_improving_ai.config import settings
from .preference_learning.schemas import timestamp


DAILY_TRIAL_LIMIT = 3.0
DAILY_BEHAVIOR_LIMIT = 2.0
SCORING_POLICY = 3
INTENSITY_POINTS = {"mild": 10, "moderate": 25, "strong": 35}
CURRENT_POINTS = {"mild": 3, "moderate": 5, "strong": 7}
CURRENT_DAYS = 7
DIRECTION = {"conservative": -1, "balanced": 0, "growth": 1}
DECLARATIONS = {"explicit_statement", "preference_change"}
CLAUSE_SPLIT = re.compile(r"[。；;，,!?！？]|但是|不过|但|\b(?:but|however)\b", re.I)
EXPOSURE_INTENT = re.compile(
    r"(?:想|希望|计划|打算|考虑|倾向于).{0,10}(?:调高|提高|增加|扩大|减少|降低|调低)"
    r".{0,10}(?:敞口|暴露|股[票权]|权益|杠杆)|"
    r"\b(?:wants?|hopes?|plans?|looking|would like)\b.{0,18}\b(?:raise|increase|add|reduce|lower|cut)\b"
    r".{0,24}\b(?:exposure|equities|equity|stock|leverage)\b", re.I)
PRINCIPAL_CONCERN = re.compile(
    r"(?:担心|在意|顾虑).{0,6}(?:本金|亏损|损失)|\b(?:worried|concerned)\b.{0,22}\b(?:principal|loss(?:es)?)\b", re.I)
RISK_WORDS = r"(?:保守|谨慎|稳健|激进|进取|风险厌恶|不想冒|不愿冒|不愿承受|愿意承受|愿意承担)"
MILD = re.compile(r"(?:有点|有些|稍微|稍|略|比较|较为|偏)\s*.{0,4}" + RISK_WORDS +
                  r"|\b(?:slightly|somewhat|a bit|rather|mildly)\s+(?:conservative|aggressive|risk.averse)\b", re.I)
STRONG = re.compile(r"(?:十分|非常|很|极其|极度|特别|相当|高度)\s*.{0,3}" + RISK_WORDS +
                    r"|\b(?:very|extremely|highly|strongly)\s+(?:conservative|aggressive|risk.averse)\b", re.I)
CLIENT_ATTITUDE = re.compile(
    r"(?:客户|他|她).{0,10}(?:不想|不愿|不太想)(?:承受|承担|冒).{0,10}(?:风险|波动)|"
    r"(?:客户|他|她).{0,8}(?:稍|略|有点|有些|较为|比较|十分|非常|极其|极度|特别)"
    r".{0,4}(?:保守|激进|进取|风险厌恶)|"
    r"\b(?:client|they|she|he)\b.{0,12}\b(?:slightly|somewhat|very|extremely|highly)"
    r"\s+(?:conservative|aggressive|risk.averse)\b", re.I)
SPECULATION = re.compile(r"如果|假[如设]|是否|是不是|可能|[？?]|\b(?:if|maybe|whether|what if)\b", re.I)
NEGATED_ATTITUDE = re.compile(r"不(?:是|算|怎么)?(?:保守|激进|进取)|\bnot (?:conservative|aggressive)\b", re.I)
RETURN_GOAL = re.compile(r"(?:追求|提高|看重|更高|高|争取).{0,8}(?:收益|回报)|(?:收益|回报).{0,8}(?:优先|重要)|"
                         r"\b(?:higher|better|more) returns?\b", re.I)
RISK_WILLINGNESS = re.compile(r"风险|本金|亏损|回撤|波动|保守|进取|激进|\b(?:risk|volatility|drawdown|loss)\b", re.I)
ACCEPTS_RISK = re.compile(
    r"(?<!不)(?:愿意|可以|能够|接受|能承受).{0,14}(?:承担|承受|冒|忍受|较大|更多|更高)?"
    r"(?:风险|亏损|回撤|波动)|"
    r"(?<!not )\b(?:willing|accepts?|tolerates?)\b.{0,35}\b(?:risk|drawdown|volatility|loss(?:es)?)\b", re.I)
IMMEDIATE = re.compile(r"现在|这阵|近期|最近|眼下|短期|\b(?:now|currently|lately|recently)\b", re.I)
DURABLE = re.compile(r"整体|长期|从今以后|一贯|以后都|\b(?:long.term|going forward|overall)\b", re.I)
ONLY_PRINCIPAL = re.compile(r"只.{0,9}(?:保住|保护|保全)(?:投入的?|投资的?)?本金|只.{0,7}保本|"
                            r"\bonly\b.{0,28}\b(?:protect|preserve) principal\b", re.I)
AGAIN_WILLING = re.compile(r"(?:又|重新|开始)敢了|\b(?:willing|ready) again\b", re.I)


def bounded(value, limit):
    return min(limit, max(-limit, value))


def classify_text(observation, policy):
    """Apply the same guardrails to new interpretations and existing stored evidence."""
    quote = unicodedata.normalize("NFKC", observation.get("evidence_text") or "")
    basis = observation.get("risk_basis")
    kind = observation["kind"]
    if not quote or (not basis and (kind != "client_cue" or policy < SCORING_POLICY)):
        return None
    if kind == "denial":
        return "denial", None
    if observation.get("assertion") != "affirmed":
        return None
    value = observation["value"]
    if policy >= SCORING_POLICY:
        if not (SPECULATION.search(quote) or DURABLE.search(quote)):
            if IMMEDIATE.search(quote) and ONLY_PRINCIPAL.search(quote):
                return "current_lower", "strong"
            if AGAIN_WILLING.search(quote):
                return "recovery", "mild"
            if basis == "current_risk_attitude" and IMMEDIATE.search(quote) and RISK_WILLINGNESS.search(quote):
                return "current", intensity(observation, quote)
            if kind in DECLARATIONS and IMMEDIATE.search(quote) and CLIENT_ATTITUDE.search(quote):
                return "current", intensity(observation, quote)
        if value == "growth" and RETURN_GOAL.search(quote) and not (
                ACCEPTS_RISK.search(quote) or EXPOSURE_INTENT.search(quote) or CLIENT_ATTITUDE.search(quote)):
            return None
    if not basis:
        return None
    clauses = CLAUSE_SPLIT.split(quote)
    concerns = [clause for clause in clauses if PRINCIPAL_CONCERN.search(clause)]
    exposure = [clause for clause in clauses if EXPOSURE_INTENT.search(clause)]
    if value == "conservative" and concerns and not any(
            CLIENT_ATTITUDE.search(clause) for clause in clauses if clause not in concerns
    ) and basis != "risk_return_tradeoff":
        if not any(re.search(r"(?:降低|减少|调低).{0,10}(?:敞口|暴露|股[票权]|权益|杠杆)", clause)
                   for clause in exposure):
            return None
    if EXPOSURE_INTENT.search(quote) and not SPECULATION.search(quote) and basis != "risk_return_tradeoff":
        if value in {"conservative", "growth"} and kind in DECLARATIONS | {"behavioral_hint"}:
            return "intent", None
    uncertain = SPECULATION.search(quote) or NEGATED_ATTITUDE.search(quote)
    if not uncertain and CLIENT_ATTITUDE.search(quote) and kind in {"unscored_observation", "behavioral_hint"}:
        # The older extractor sometimes treated a statement of aversion as a question.
        return "statement", intensity(observation, quote)
    if kind in DECLARATIONS and basis in {"declared_attitude", "risk_return_tradeoff"}:
        return None if uncertain else ("statement", intensity(observation, quote))
    if basis == "allocation_intent" and not SPECULATION.search(quote):
        return "intent", None
    if basis == "observed_risk_choice":
        return "choice", None
    if basis == "directed_inquiry" and observation["entry_point"] != "advisor_note":
        return "inquiry", None
    return None


def intensity(observation, quote):
    if STRONG.search(quote):
        return "strong"
    if MILD.search(quote):
        return "mild"
    reported = observation.get("risk_intensity")
    if reported in INTENSITY_POINTS:
        return reported
    return "moderate"


def risk_index(observations, calibration, *, work_date=None, policy=SCORING_POLICY):
    zone = ZoneInfo(settings.portfolio_work_timezone)
    today = work_date or datetime.now(zone).date()
    statements = defaultdict(list)
    trials, behaviors = [], []
    denials = []
    recent = None
    last_explicit = None
    for row in observations:
        at = timestamp(row["occurred_at"])
        event = row["source_event_id"]
        value = row["value"]
        if row["kind"] == "allocation_exploration":
            basis = row.get("basis") or {}
            current, target = basis.get("current_stock_weight"), basis.get("target_stock_weight")
            if all(type(v) in (int, float) and 0 <= v <= 1 for v in (current, target)):
                manual = basis.get("parameter_source") == "manual"
                weight = (2 if manual else .25) * min(1, abs(target - current) / .20)
                trials.append((at, row["evidence_group_id"], event, manual, DIRECTION[value] * weight))
            continue
        classification = classify_text(row, policy)
        if not classification:
            continue
        role, strength = classification
        if role in {"current", "current_lower", "recovery"}:
            if role == "recovery":
                prior = recent[0] if recent is not None else last_explicit
                if prior is None or not 0 <= (at.astimezone(zone).date() - prior.astimezone(zone).date()).days < CURRENT_DAYS:
                    continue
                value = "growth"
            elif role == "current_lower":
                value = "conservative"
            if value in DIRECTION:
                recent = (at, value, strength, row)
            continue
        if role == "denial":
            denials.append((at, value))
            continue
        if row["kind"] == "unscored_observation" and role == "statement":
            value = ("growth" if re.search(r"激进|进取|\baggressive\b", row["evidence_text"], re.I) else "conservative")
        if value not in DIRECTION:
            continue
        if role == "statement":
            statements[event].append((at, value, strength, row))
            last_explicit = at
        elif DIRECTION[value]:
            weight = {"inquiry": 1, "intent": 2, "choice": 2}[role] * DIRECTION[value]
            behaviors.append((at, row["evidence_group_id"], event, role, weight, row["evidence_text"]))

    declaration = None
    disputed = False
    preference_start = None
    for entries in statements.values():
        if len({value for _, value, _, _ in entries}) > 1:
            disputed = True
            continue
        declaration = max(entries, key=lambda item: INTENSITY_POINTS[item[2]])
        disputed = False
        if declaration[3]["kind"] == "preference_change":
            preference_start = declaration[0]
    anchor = 50
    anchor_source = anchor_intensity = None
    used_events = set()
    if declaration:
        at, value, anchor_intensity, row = declaration
        anchor = 50 + DIRECTION[value] * INTENSITY_POINTS[anchor_intensity]
        anchor_source = row["evidence_text"]
        used_events.add(row["source_event_id"])
        disputed |= any(when > at and denied == value for when, denied in denials)

    trial_groups = {}
    for at, group, event, manual, points in trials:
        if preference_start and at < preference_start:
            continue
        key = (at.astimezone(zone).date(), group)
        previous = trial_groups.get(key)
        if previous is None or manual or not previous[0]:
            trial_groups[key] = (manual, points, event)
    trial_days = defaultdict(float)
    for (day, _), (_, points, event) in trial_groups.items():
        trial_days[day] += points
        used_events.add(event)

    behavior_events = defaultdict(list)
    for at, group, event, role, points, quote in behaviors:
        if preference_start and at < preference_start:
            continue
        behavior_events[(at.astimezone(zone).date(), group, role, event)].append((points, quote))
    behavior_groups = {}
    for (day, group, role, event), entries in behavior_events.items():
        if len({points for points, _ in entries}) == 1:
            points, quote = entries[-1]
            behavior_groups[(day, group, role)] = (points, event, " ".join(quote.casefold().split()))
    behavior_days = defaultdict(float)
    repeated_text = set()
    for (day, _, role), (points, event, quote) in behavior_groups.items():
        key = (day, role, quote)
        if key in repeated_text:
            continue
        repeated_text.add(key)
        behavior_days[day] += points
        used_events.add(event)

    def sum_points(days, daily_limit, overall_limit):
        raw = sum(bounded(p, daily_limit) for p in days.values())
        today_raw = bounded(days.get(today, 0), daily_limit)
        total = overall_limit * math.tanh(raw / overall_limit)
        return total, total - overall_limit * math.tanh((raw - today_raw) / overall_limit)

    trial_points, today_trial = sum_points(trial_days, DAILY_TRIAL_LIMIT, 8)
    behavior_points, today_behavior = sum_points(behavior_days, DAILY_BEHAVIOR_LIMIT, 12)
    current_points = 0
    current_source = None
    if recent and (declaration is None or recent[0] > declaration[0]):
        at, value, strength, row = recent
        age = (today - at.astimezone(zone).date()).days
        if 0 <= age < CURRENT_DAYS:
            current_points = DIRECTION[value] * CURRENT_POINTS[strength]
            current_source = row["evidence_text"]
            used_events.add(row["source_event_id"])
    reference = min(100, max(0, anchor + trial_points + behavior_points + current_points))
    chosen = calibration.get("chosen")
    effective = reference
    if chosen is not None:
        movement = 0 if calibration.get("mode") == "fixed" else reference - calibration["baseline"]
        effective = min(100, max(0, chosen + movement))
    return {
        "effective": effective, "reference": reference, "calibration": calibration,
        "groups": len(used_events),
        "breakdown": {"anchor": anchor, "trial_points": trial_points, "behavior_points": behavior_points,
                      "current_points": current_points, "current_source": current_source, "current_days":CURRENT_DAYS,
                      "today_trial": today_trial, "today_behavior": today_behavior,
                      "daily_trial_limit": DAILY_TRIAL_LIMIT, "daily_behavior_limit": DAILY_BEHAVIOR_LIMIT,
                      "work_timezone": zone.key},
        "anchor_source": anchor_source, "anchor_intensity": anchor_intensity,
        "status": "conflict" if disputed else "scored" if used_events else "insufficient",
        "scoring_event_ids": sorted(used_events), "legacy_review": False,
    }
