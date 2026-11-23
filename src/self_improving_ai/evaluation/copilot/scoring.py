"""Score one copilot run (its trace and final answer) against a golden case.

A run is the dict returned by run_copilot: steps (tool or sub-agent calls in order, each with
action and args) and response_parts (the advisor sees the last one).

Stages, so a failure points at its cause:
  tools     every required tool or sub-agent was called (name only)
  args      each required call used the right arguments (for example the right client_id)
  order     required calls happened in the expected order (only for order = "strict")
  forbidden no forbidden tool or sub-agent was called
  calls     the number of calls stayed within max_calls (reported, not part of pass)
  facts     share of expected facts found in the final answer
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Callable

STAGES = ("tools", "args", "order", "forbidden", "calls", "facts")

# Returns calendar events (dicts with date and start_time) for one client.
EventsLookup = Callable[[str], list]

_NUM = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*([kKmM]\b)?")


def numbers_in(text: str) -> list[float]:
    """All numbers in text, reading $147K as 147000 and 1.2M as 1200000."""
    out = []
    for raw, suffix in _NUM.findall(text):
        value = float(raw.replace(",", ""))
        if suffix:
            value *= 1_000 if suffix.lower() == "k" else 1_000_000
        out.append(value)
    return out


def _date_forms(iso: str) -> list[str]:
    d = date.fromisoformat(iso)
    month, short = d.strftime("%B"), d.strftime("%b")
    return [iso, f"{month} {d.day}", f"{short} {d.day}", f"{d.day} {month}", f"{d.month}/{d.day}"]


def _upcoming(events: list, today: date) -> list:
    return sorted((e for e in events if str(e.get("date", "")) >= today.isoformat()),
                  key=lambda e: (str(e["date"]), str(e.get("start_time", ""))))


_NONE_UPCOMING = ["no upcoming", "no meetings", "nothing scheduled", "don't see any", "no scheduled"]


def resolve_fact(fact: dict, events_for: EventsLookup | None, today: date) -> dict:
    """Turn a dynamic fact into a static one using live calendar data and the run date.

    next_event          the date of one client's next event
    events_window       every client with an event in the next `days` days (names must all appear)
    next_client_total   name and portfolio total of the client met next; when that meeting is today,
                        the following client is also accepted, since the prompt has the date but not the time
    """
    kind = fact["type"]
    lookup = events_for or (lambda _cid: [])
    if kind == "next_event":
        upcoming = [e for e in _upcoming(lookup(fact["client_id"]), today)
                    if not fact.get("time") or e.get("start_time") == fact["time"]]
        if not upcoming:
            return {"type": "text", "any": _NONE_UPCOMING, "why": f"No {fact['client_id']} event on or after {today}"}
        nxt = upcoming[0]
        forms = _date_forms(str(nxt["date"])) + (["today"] if str(nxt["date"]) == today.isoformat() else [])
        return {"type": "text", "any": forms,
                "why": f"{fact.get('why', 'Next event')}: {nxt.get('title')} on {nxt['date']} {nxt.get('start_time', '')}"}
    if kind == "events_window":
        end = (today + timedelta(days=fact["days"])).isoformat()
        names = [c[0] for cid, c in fact["clients"].items()
                 if any(str(e["date"]) <= end for e in _upcoming(lookup(cid), today))]
        if not names:
            return {"type": "text", "any": _NONE_UPCOMING, "why": f"No client events from {today} to {end}"}
        return {"type": "all", "all": names, "why": f"Clients with events from {today} to {end}: {', '.join(names)}"}
    if kind == "next_client_total":
        firsts = []
        for cid, (name, total) in fact["clients"].items():
            ups = _upcoming(lookup(cid), today)
            if ups:
                firsts.append((str(ups[0]["date"]), str(ups[0].get("start_time", "")), name, total))
        firsts.sort()
        if not firsts:
            return {"type": "text", "any": _NONE_UPCOMING, "why": f"No client events on or after {today}"}
        accepted = firsts[:2] if firsts[0][0] == today.isoformat() and len(firsts) > 1 else firsts[:1]
        options = [{"type": "every", "facts": [{"type": "text", "any": [name]},
                                               {"type": "number", "value": total, "tol": 0.005}]}
                   for _d, _t, name, total in accepted]
        return {"type": "one_of", "facts": options,
                "why": "Next client and portfolio total: " + " or ".join(f"{n} {t}" for _d, _t, n, t in accepted)}
    return fact


def fact_found(fact: dict, answer: str) -> bool:
    if fact["type"] == "all":
        low = answer.lower()
        return all(s.lower() in low for s in fact["all"])
    if fact["type"] == "every":
        return all(fact_found(f, answer) for f in fact["facts"])
    if fact["type"] == "one_of":
        return any(fact_found(f, answer) for f in fact["facts"])
    if fact["type"] == "text":
        low = answer.lower()
        return any(s.lower() in low for s in fact["any"])
    if fact["type"] == "number":
        target, tol = float(fact["value"]), float(fact.get("tol", 0.01))
        return any(abs(n - target) <= max(abs(target) * tol, 1e-9) for n in numbers_in(answer))
    raise ValueError(f"Unknown fact type: {fact['type']}")


def _args_match(expected: dict, actual: dict) -> bool:
    for key, want in (expected or {}).items():
        if key == "_mentions":
            blob = json.dumps(actual, default=str).lower()
            if not any(m.lower() in blob for m in want):
                return False
        elif str(actual.get(key, "")).strip().upper() != str(want).strip().upper():
            return False
    return True


def _names(step: dict) -> list[str]:
    return step["tool"] if isinstance(step["tool"], list) else [step["tool"]]


@dataclass
class CaseScore:
    case_id: str
    category: str
    stages: dict = field(default_factory=dict)
    passed: bool = False
    plan_passed: bool = False
    calls: list = field(default_factory=list)
    answer: str = ""
    missing_facts: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def score_case(case: dict, run: dict, *, events_for: EventsLookup | None = None,
               today: date | None = None) -> CaseScore:
    today = today or date.today()
    plan = case["plan"]
    steps = run.get("steps", [])
    calls = [(s.get("action", ""), s.get("args") or {}) for s in steps]
    answer = (run.get("response_parts") or [""])[-1]
    sc = CaseScore(case["id"], case["category"], calls=[c[0] for c in calls], answer=answer)

    required = [s for s in plan["steps"] if not s.get("optional")]
    tool_hits = 0
    found_at = []  # (position in plan, index of the matching call)
    args_ok = True
    for pos, step in enumerate(plan["steps"]):
        names = _names(step)
        name_idx = [i for i, (n, _) in enumerate(calls) if n in names]
        full_idx = [i for i in name_idx if _args_match(step.get("args"), calls[i][1])]
        if step.get("optional"):
            if full_idx:
                found_at.append((pos, full_idx[0]))
            continue
        if name_idx:
            tool_hits += 1
        if full_idx:
            found_at.append((pos, full_idx[0]))
        else:
            args_ok = False
            if name_idx:
                sc.notes.append(f"{'/'.join(names)} called with unexpected args: {calls[name_idx[0]][1]}")
            else:
                sc.notes.append(f"missing call: {'/'.join(names)}")

    sc.stages["tools"] = tool_hits / len(required) if required else 1.0
    sc.stages["args"] = 1.0 if args_ok else 0.0
    order_ok = True
    if plan.get("order") == "strict":
        idx = [i for _, i in sorted(found_at)]
        order_ok = idx == sorted(idx)
        if not order_ok:
            sc.notes.append("calls out of the expected order")
    sc.stages["order"] = 1.0 if order_ok else 0.0
    bad = sorted({n for n, _ in calls if n in plan.get("forbidden", [])})
    if bad:
        sc.notes.append(f"forbidden calls: {bad}")
    sc.stages["forbidden"] = 0.0 if bad else 1.0
    sc.stages["calls"] = 1.0 if len(calls) <= plan.get("max_calls", 99) else 0.0
    if not sc.stages["calls"]:
        sc.notes.append(f"{len(calls)} calls, budget {plan.get('max_calls')}")

    facts = [resolve_fact(f, events_for, today) for f in case.get("facts", [])]
    hits = [fact_found(f, answer) for f in facts]
    sc.missing_facts = [f.get("why", str(f)) for f, ok in zip(facts, hits) if not ok]
    sc.stages["facts"] = sum(hits) / len(hits) if hits else 1.0

    sc.plan_passed = sc.stages["tools"] == 1.0 and args_ok and order_ok and not bad
    sc.passed = sc.plan_passed and sc.stages["facts"] == 1.0
    return sc


def summarize(scores: list[CaseScore]) -> dict:
    n = len(scores) or 1
    by_cat: dict[str, list[CaseScore]] = {}
    for s in scores:
        by_cat.setdefault(s.category, []).append(s)
    return {
        "cases": len(scores),
        "pass_rate": sum(s.passed for s in scores) / n,
        "plan_pass_rate": sum(s.plan_passed for s in scores) / n,
        "stages": {k: sum(s.stages.get(k, 0) for s in scores) / n for k in STAGES},
        "by_category": {c: {"cases": len(v), "pass_rate": sum(s.passed for s in v) / len(v)}
                        for c, v in sorted(by_cat.items())},
    }
