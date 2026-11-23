"""Golden-set scoring of live copilot traces."""

import json
from datetime import date
from pathlib import Path

import pytest

from self_improving_ai.evaluation.copilot.scoring import fact_found, numbers_in, score_case, summarize

GOLDEN = Path(__file__).resolve().parents[2] / "src/self_improving_ai/data/orchestrator/golden_copilot.json"
CASES = {c["id"]: c for c in json.loads(GOLDEN.read_text())["cases"]}
TODAY = date(2026, 11, 17)
GARCIA_EVENTS = [
    {"date": "2026-11-12", "start_time": "14:00", "title": "Q4 Review: Garcia Family"},
    {"date": "2026-12-10", "start_time": "14:00", "title": "Annual Review: Garcia Family"},
]


def events_for(client_id):
    return GARCIA_EVENTS if client_id == "CLI-001" else []


def run(calls, answer):
    return {"steps": [{"action": n, "args": a} for n, a in calls], "response_parts": [answer]}


def test_golden_set_is_well_formed():
    assert len(CASES) >= 20
    for case in CASES.values():
        assert case["question"] and case["verification"] and case["facts"]
        assert case["plan"]["order"] in ("any", "strict")
        for step in case["plan"]["steps"]:
            assert step["tool"]


@pytest.mark.parametrize("text,value", [("about $147K", 147000), ("$146,750 total", 146750),
                                        ("21.6% in stocks", 21.6), ("$1.2M", 1_200_000)])
def test_numbers_are_read_in_advisor_formats(text, value):
    assert value in numbers_in(text)


def test_correct_run_passes():
    sc = score_case(CASES["G05"], run([("get_client_holdings", {"client_id": "CLI-001"})],
                    "The Garcias hold about $146,750, mostly VTSAX, plus 100 shares of AAPL."),
                    events_for=events_for, today=TODAY)
    assert sc.passed and sc.stages["facts"] == 1.0


def test_wrong_client_fails_args_not_tools():
    sc = score_case(CASES["G05"], run([("get_client_holdings", {"client_id": "CLI-002"})], "$142,700 VTSAX AAPL"),
                    events_for=events_for, today=TODAY)
    assert sc.stages["tools"] == 1.0 and sc.stages["args"] == 0.0 and not sc.passed


def test_next_event_uses_the_run_date():
    case = CASES["G01"]
    calls = [("get_upcoming_events", {"client_id": "CLI-001"})]
    assert score_case(case, run(calls, "Your next Garcia meeting is December 10 at 2pm."),
                      events_for=events_for, today=TODAY).passed
    # The November 12 meeting is already past on the run date.
    assert not score_case(case, run(calls, "It's on November 12."), events_for=events_for, today=TODAY).passed


def test_strict_order_and_briefing_mentions():
    case = CASES["G14"]
    good = [("get_upcoming_events", {"client_id": "CLI-001"}),
            ("meeting_briefing_agent", {"request": "Brief for CLI-001 on 2026-12-10 14:00"})]
    answer = "Your Dec 10 brief is ready in Meeting Prep."
    assert score_case(case, run(good, answer), events_for=events_for, today=TODAY).passed
    sc = score_case(case, run(list(reversed(good)), answer), events_for=events_for, today=TODAY)
    assert sc.stages["order"] == 0.0 and not sc.passed


def test_forbidden_call_fails_domain_question():
    sc = score_case(CASES["G04"], run([("get_client_info", {"client_id": "CLI-001"})], "Withdrawals are tax-free."),
                    today=TODAY)
    assert sc.stages["forbidden"] == 0.0 and not sc.passed
    assert score_case(CASES["G04"], run([], "Qualified withdrawals are tax-free."), today=TODAY).passed


def test_any_of_tools_and_summary():
    ok = score_case(CASES["G18"], run([("get_client_info", {"client_id": "CLI-005"})], "About $4,200 a month."),
                    today=TODAY)
    missing = score_case(CASES["G18"], run([], "Not sure."), today=TODAY)
    assert ok.passed and not missing.passed and missing.stages["tools"] == 0.0
    summary = summarize([ok, missing])
    assert summary["pass_rate"] == 0.5 and summary["by_category"]["crm"]["cases"] == 2


def test_fact_found_text_is_case_insensitive():
    assert fact_found({"type": "text", "any": ["ARKK"]}, "they own arkk")


def test_runner_writes_traces_and_report(tmp_path, monkeypatch):
    import asyncio

    from self_improving_ai.evaluation.copilot import runner as mod

    async def fake_run_one(_runner, case, _timeout):
        if case["id"] == "G05":
            return {**run([("get_client_holdings", {"client_id": "CLI-001"})], "$146,750 in VTSAX and AAPL"),
                    "error": None, "seconds": 1.0}
        return {"steps": [], "response_parts": [], "error": "TimeoutError", "seconds": 300.0}

    monkeypatch.setattr(mod, "run_one", fake_run_one)
    cases = [CASES["G05"], CASES["G13"]]
    summary = asyncio.run(mod.run_golden(cases, tmp_path, runner=object(), events_for=events_for, today=TODAY))
    assert summary["pass_rate"] == 0.5 and summary["errors"] == 1
    assert len((tmp_path / "traces.jsonl").read_text().splitlines()) == 2
    assert "1/2 passed" in (tmp_path / "report.md").read_text()


# Hard cases: date windows, next-client chains and composite facts.
from datetime import date as _date  # noqa: E402

from self_improving_ai.evaluation.copilot.scoring import fact_found, resolve_fact  # noqa: E402

_EVENTS = {"CLI-004": [{"date": "2026-11-20", "start_time": "15:30"}],
           "CLI-005": [{"date": "2026-11-22", "start_time": "09:00"}, {"date": "2026-12-05", "start_time": "09:30"}],
           "CLI-001": [{"date": "2026-11-12", "start_time": "14:00"}, {"date": "2026-12-10", "start_time": "14:00"}]}
_CLIENTS = {"CLI-001": ["Garcia", 146750], "CLI-004": ["Okafor", 32000], "CLI-005": ["Patel", 102100]}


def _lookup(cid):
    return _EVENTS.get(cid, [])


def test_events_window_needs_every_client_in_range():
    fact = resolve_fact({"type": "events_window", "days": 14, "clients": {c: [n] for c, (n, _) in _CLIENTS.items()}},
                        _lookup, _date(2026, 11, 20))
    assert fact["all"] == ["Okafor", "Patel"]
    assert fact_found(fact, "You meet the Okafors today and Priya Patel on Nov 22.")
    assert not fact_found(fact, "Only the Okafors.")


def test_next_client_accepts_the_following_client_when_the_meeting_is_today():
    fact = resolve_fact({"type": "next_client_total", "clients": _CLIENTS}, _lookup, _date(2026, 11, 20))
    assert fact_found(fact, "Next is the Okafor Family, with $32,000 invested.")
    assert fact_found(fact, "Your 3:30 already passed; next is Patel at $102,100.")
    assert not fact_found(fact, "Next is Patel.")  # name without the total
    later = resolve_fact({"type": "next_client_total", "clients": _CLIENTS}, _lookup, _date(2026, 11, 21))
    assert not fact_found(later, "Okafor, $32,000")
