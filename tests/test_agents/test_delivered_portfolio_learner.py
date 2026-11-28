"""Regression coverage for the audited delivery through the real Portfolio A API."""

import asyncio
from io import BytesIO
import json
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

from self_improving_ai.agents.portfolio import application, learning
from self_improving_ai.agents.portfolio.preference_learning import PortfolioLearner
from self_improving_ai.agents.portfolio.preference_learning import extract_signals, provider_backoff
from self_improving_ai.agents.portfolio.preference_learning.adapter import create_adapter
from self_improving_ai.agents.portfolio.preference_learning.parser import GeminiParser
from self_improving_ai.agents.portfolio.service import run_portfolio_agent
from self_improving_ai.config import settings
from self_improving_ai.dashboard.app import app
from tests.test_agents.test_portfolio_agent import ScriptedModel

CLIENT = "CLI-001"
API = f"/api/portfolio/learning/{CLIENT}"
STATEMENT = "The client is conservative."
ACCOUNT = "The education account should take less risk."
FACTORY = "self_improving_ai.agents.portfolio.preference_learning.adapter:create_adapter"


def signal(text, *, kind="explicit_statement", dimension="risk_tilt", value="conservative", **extra):
    return {"kind": kind, "dimension": dimension, "value": value, "assertion": "affirmed",
            "evidence_text": text, "reason": "Synthetic fixture", "scope": None,
            "risk_basis": "declared_attitude" if dimension == "risk_tilt" else "no_direction", **extra}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("SIA_PORTFOLIO_LEARNER_FACTORY", FACTORY)
    monkeypatch.setenv("SIA_PORTFOLIO_LEARNING_DIR", str(tmp_path))
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.adapter.PortfolioParser.parse_scoped",
                        lambda _, text, scope: [signal(text)] if text == STATEMENT else [])
    return TestClient(app)


def note(api, text, event_id, **extra):
    result = api.post(f"{API}/notes", json={"message": text, "observation": {"event_id": event_id}, **extra})
    result.raise_for_status()
    return result.json()


def calculate(api, event_id):
    result = api.post("/api/portfolio/execute", json={
        "task": "allocation", "client_id": CLIENT, "observation": {"event_id": event_id},
    })
    result.raise_for_status()
    return result.json()


def test_configured_factory_uses_delivered_learner_and_preserves_actual_calculation(api):
    run = calculate(api, "calculation")
    assert run["status"] == "success"
    assert run["learning"]["status"] == "connected"
    assert run["result"]["asset_classes"]["stock"]["target_weight"] == pytest.approx(.6)
    note(api, STATEMENT, "statement")
    context = api.get(API).json()["context"]
    assert context["team_declared"][0]["value"] == "conservative"
    assert api.get("/api/portfolio/learning/CLI-002").json()["context"]["team_declared"] == []
    index = context["working_risk_index"]
    assert 49.75 <= index["automatic"] < 50
    assert index["reference"] == pytest.approx(25 + index["breakdown"]["trial_points"])
    assert PortfolioLearner(learning.workspace() / "learner").get_context(CLIENT) == {
        k:v for k,v in context.items() if k != "working_risk_index"}
    assert api.post(f"{API}/retract/statement").json()["context"]["team_declared"] == []


def test_conflict_evidence_resolves_old_records_in_the_selected_client(api):
    note(api, STATEMENT, "old-declaration")
    higher = api.post("/api/portfolio/execute", json={
        "task": "allocation", "client_id": CLIENT, "target_stock_weight": .95,
        "observation": {"event_id": "higher-trial", "parameter_sources": {"target_stock_weight": "manual"}},
    })
    higher.raise_for_status()
    for i in range(10):
        response = api.post("/api/portfolio/execute", json={
            "task": "holdings", "client_id": CLIENT, "observation": {"event_id": f"holdings-{i}"},
        })
        response.raise_for_status()
    first = api.get(API).json()
    assert all(row["event_id"] != "old-declaration" for row in first["events"])
    conflict = next(c for c in first["context"]["conflicts"] if c["kind"] == "exploration_differs_from_declaration")
    declaration, trial = (first["conflict_sources"]["signals"][ref] for ref in conflict["evidence_ids"])
    assert declaration["event_id"] == "old-declaration" and declaration["evidence_text"] == STATEMENT
    assert trial["event_id"] == "higher-trial" and trial["basis"]["target_stock_weight"] == .95
    located = api.get(API, params={"event_id": "old-declaration"}).json()
    assert located["located_event"] and located["pagination"]["page"] == 2
    assert any(row["event_id"] == "old-declaration" for row in located["events"])
    number = first["source_operation_numbers"]["old-declaration"]
    assert number.startswith("UNI-")
    numbered = api.get(API, params={"operation_number": number}).json()
    assert numbered["located_event_id"] == "old-declaration" and numbered["pagination"]["page"] == 2
    assert next(row["operation_number"] for row in numbered["events"] if row["event_id"] == "old-declaration") == number
    other = api.get("/api/portfolio/learning/CLI-002", params={"event_id": "old-declaration"}).json()
    assert not other["located_event"] and "conflict_sources" not in other


def test_operation_number_survives_outage_retry_reinterpretation_and_withdrawal(api, monkeypatch):
    learner = learning.adapter_for(FACTORY, str(learning.workspace())).learner
    parser = learner.parser
    learner.parser = None
    pending = note(api, STATEMENT, "same-input")
    assert pending["learning"]["status"] == "processing_pending"
    initial = api.get(API).json()
    number = initial["events"][0]["operation_number"]
    assert initial["context"] is None and number.startswith("UNI-")

    note(api, STATEMENT, "same-input")
    waiting = api.get(API, params={"operation_number": number}).json()
    assert waiting["total_events"] == 1 and waiting["located_event_id"] == "same-input"
    learner.parser = parser
    api.post(f"{API}/retry").raise_for_status()
    api.post(f"{API}/reinterpret/same-input", json={"revision": 2}).raise_for_status()
    updated = api.get(API).json()["events"][0]
    assert updated["revision"] == 3 and updated["operation_number"] == number
    api.post(f"{API}/retract/same-input").raise_for_status()
    withdrawn = api.get(API, params={"operation_number": number}).json()
    assert {row["kind"] for row in withdrawn["events"]} == {"event", "retract"}
    assert {row["operation_number"] for row in withdrawn["events"]} == {number}
    assert api.get(f"{API}/export").json()["operation_numbers"] == {"same-input": number}

    other = "/api/portfolio/learning/CLI-002"
    api.post(other + "/notes", json={"message": STATEMENT, "observation": {"event_id": "same-input"}}).raise_for_status()
    other_number = api.get(other).json()["events"][0]["operation_number"]
    assert other_number != number
    assert not api.get(other, params={"operation_number": number}).json()["located_event"]

    api.post(f"{API}/clear", json={"confirm": True, "generation": withdrawn["generation"]}).raise_for_status()
    note(api, STATEMENT, "same-input")
    assert not api.get(API, params={"operation_number": number}).json()["located_event"]
    assert api.get(API).json()["events"][0]["operation_number"] not in {number, other_number}
    assert api.get(other).json()["events"][0]["operation_number"] == other_number


def test_duplicate_pending_update_stays_pending_then_reparses_on_retry(api):
    learner = learning.adapter_for(FACTORY, str(learning.workspace())).learner
    parser = learner.parser
    learner.parser = None
    note(api, STATEMENT, "pending")
    assert api.post(f"{API}/retry").json()["pending_count"] == 1
    # A status-only delivery attempt with the same event cannot mark it applied.
    assert learning.Journal().sync(CLIENT) == "processing_pending"
    with learning.Journal().db() as db:
        db.execute("UPDATE deliveries SET status='applied' WHERE status='pending'")
    learner.parser = parser
    retried = api.post(f"{API}/retry").json()
    assert retried["pending_count"] == 0
    assert retried["context"]["team_declared"][0]["value"] == "conservative"
    version = retried["context"]["profile_version"]
    assert api.post(f"{API}/retry").json()["context"]["profile_version"] == version
    stored = learner.export_client(CLIENT)["evidence_history"]["extractions"]
    assert len(stored) == 1 and stored[0]["status"] == "ok" and stored[0]["event"]["revision"] == 2


def test_undelivered_retraction_persists_and_unblocks_later_input(api, monkeypatch):
    with monkeypatch.context() as offline:
        offline.delenv("SIA_PORTFOLIO_LEARNER_FACTORY")
        calculate(api, "unsent")
        assert api.post(f"{API}/retract/unsent").json()["pending_count"] == 1
    assert calculate(api, "next")["learning"]["status"] == "connected"
    view = api.get(API).json()
    assert view["pending_count"] == 0
    learner = PortfolioLearner(learning.workspace() / "learner")
    history = learner.export_client(CLIENT)["evidence_history"]
    assert history["retracted_event_ids"] == ["unsent"]
    assert [x["event"]["event_id"] for x in history["extractions"]] == ["next"]
    original = next(r for r in learning.Journal().rows(CLIENT) if r["event"] == "unsent" and r["revision"] == 2)
    assert learner.process_event(json.loads(original["payload"]))["issues"][0]["code"] == "event_retracted"


def test_failed_agent_calculation_can_recover_preference_on_retry(api, monkeypatch):
    learner = learning.adapter_for(FACTORY, str(learning.workspace())).learner
    parser = learner.parser
    learner.parser = None

    async def failed(*args, **kwargs):
        return {"run_id": "failed-test-run", "status": "error", "trace": [], "result": {"status": "error"}}

    monkeypatch.setattr(application, "run_portfolio_agent", failed)
    run = api.post("/api/portfolio/agent", json={
        "client_id": CLIENT, "message": STATEMENT, "observation": {"event_id": "failed-analysis"},
    }).json()
    assert run["status"] == "error" and run["learning"]["status"] == "processing_pending"
    failed_record = api.get(API).json()
    assert failed_record["context"] is None
    number = failed_record["events"][0]["operation_number"]
    assert number.startswith("UNI-")
    learner.parser = parser
    view = api.post(f"{API}/retry").json()
    assert view["pending_count"] == 0
    assert view["context"]["team_declared"][0]["value"] == "conservative"
    assert view["events"][0]["operation_number"] == number


def test_completed_agent_request_reuses_validated_text_and_keeps_queue_moving(api, monkeypatch):
    learner = learning.adapter_for(FACTORY, str(learning.workspace())).learner
    calls = []

    def flaky_parser(text):
        calls.append(text)
        if len(calls) > 1:
            raise TimeoutError("Synthetic second-call timeout")
        return [signal(text)]

    async def finished(*args, **kwargs):
        return {"run_id": "completed-test-run", "status": "success", "response": "Synthetic answer",
                "trace": []}

    learner.parser = flaky_parser
    monkeypatch.setattr(application, "run_portfolio_agent", finished)
    result = api.post("/api/portfolio/agent", json={
        "client_id": CLIENT, "message": STATEMENT, "observation": {"event_id": "same-text"},
    })
    result.raise_for_status()
    run = result.json()
    assert run["learning_context_used"]["context"]["team_declared"][0]["value"] == "conservative"
    assert run["learning"]["status"] == "connected"
    assert calculate(api, "later-allocation")["learning"]["status"] == "connected"
    assert calls == [STATEMENT]

    view = api.get(API).json()
    assert view["pending_count"] == 0
    saved = next(record for record in view["events"] if record["event_id"] == "same-text")
    extraction = saved["update"]["extraction"]
    assert extraction["parser"]["reused_from_revision"] == 1
    assert extraction["signals"][0]["source_revision"] == 2
    assert extraction["event"]["result_reference"] == "completed-test-run"
    assert view["context"]["team_declared"][0]["source_event_id"] == "same-text"


@pytest.mark.parametrize("changed_fields", [
    {"user_text": "The client now prefers higher risk."},
    {"scope": {"account_id": "ACC-001A"}},
])
def test_changed_text_or_scope_forces_a_new_parse(tmp_path, changed_fields):
    calls = []

    class ScopedParser:
        def parse_scoped(self, text, scope):
            calls.append((text, scope))
            return [signal(text, value="growth" if "higher risk" in text else "conservative")]

    learner = PortfolioLearner(tmp_path, parser=ScopedParser())
    start = learning.event_from(CLIENT, "advisor_request", {}, {"event_id": "changed"}, STATEMENT, "agent")
    assert learner.process_event(start)["status"] == "ok"
    completed = {**start, "revision": 2, "outcome": "success", **changed_fields}
    result = learner.process_event(completed)
    assert result["status"] == "ok"
    assert calls == [(STATEMENT, None), (completed["user_text"], completed["scope"])]
    assert "reused_from_revision" not in result["extraction"]["parser"]


def test_historical_correction_remains_reviewable_without_blocking_valid_update(api):
    learner = learning.adapter_for(FACTORY, str(learning.workspace())).learner
    text = "Withdraw MISSING-01 because it was incorrect."
    learner.parser = lambda message: [
        signal(text, kind="correction", dimension="correction", value=None, target_event_id="MISSING-01")
    ] if message == text else [signal(message)]
    note(api, text, "missing-correction")
    note(api, STATEMENT, "later-statement")
    view = api.get(API).json()
    assert view["pending_count"] == 0 and view["review_count"] == 1
    statuses = {r["event_id"]: r["delivery_status"] for r in view["events"]}
    assert statuses == {"missing-correction": "needs_review", "later-statement": "applied"}
    assert view["context"]["team_declared"][0]["source_event_id"] == "later-statement"


def test_verified_account_reaches_provider_and_unknown_scope_is_reviewable(api, monkeypatch):
    parser = GeminiParser("gemini-test", api_key="synthetic")
    opener = MagicMock()

    def fake_provider(request, *, timeout):
        payload = json.loads(request.data)
        schema = payload["generationConfig"]["responseJsonSchema"]
        scope_schema = schema["properties"]["signals"]["items"]["properties"]["scope"]
        assert scope_schema["properties"]["applicability"]["enum"] == ["ongoing", "this_analysis_only"]
        assert scope_schema["additionalProperties"] is False
        instructions = payload["systemInstruction"]["parts"][0]["text"]
        scope = {"account_id": "ACC-001A"} if '"account_id": "ACC-001A"' in instructions else {"unresolved": True}
        reply = signal(ACCOUNT, scope=scope)
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"candidates": [
            {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps([reply])}]}}
        ]}).encode()
        return response

    opener.open.side_effect = fake_provider
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.parser.request.build_opener",
                        lambda *_: opener)
    learner = learning.adapter_for(FACTORY, str(learning.workspace())).learner
    learner.parser = parser
    note(api, ACCOUNT, "unbound")
    assert api.get(API).json()["review_count"] == 1
    denied = api.post(f"{API}/notes", json={"message": ACCOUNT, "account_id": "ACC-002A"})
    assert denied.status_code == 409
    note(api, ACCOUNT, "bound", account_id="ACC-001A")
    assert api.get(API).json()["context"]["team_declared"] == []
    scoped = learner.get_context(CLIENT, {"account_id": "ACC-001A"})
    assert scoped["team_declared"][0]["source_event_id"] == "bound"
    assert opener.open.call_count == 2


@pytest.mark.parametrize("scope, status", [
    ({"account_id": None, "goal_id": None, "unresolved": False, "applicability": "long_term"}, "ok"),
    ({"unresolved": True, "applicability": "long_term"}, "needs_review"),
    ({"account_id": "invented", "unresolved": False, "applicability": "long_term"}, "partial"),
])
def test_observed_legacy_scope_shape_preserves_account_validation(scope, status):
    event = learning.event_from(CLIENT, "client_statement", {}, {}, STATEMENT, "advisor_note")
    result = extract_signals(event, lambda text: [signal(text, scope=scope)])
    assert result["status"] == status
    if status == "ok":
        assert result["signals"][0]["scope"] is None
    else:
        assert not result["signals"]


def test_provider_quota_cooldown_preserves_pending_note_and_recovers_on_retry(api, monkeypatch):
    model, credential = "gemini-quota-fixture", "synthetic-shared-quota-key"
    monkeypatch.setenv("SIA_PORTFOLIO_API_KEY", credential)
    monkeypatch.setattr(settings, "portfolio_model", model)
    monkeypatch.setattr(provider_backoff, "_waits", {})
    now = [1_800_000_000]
    monkeypatch.setattr(provider_backoff, "time", lambda: now[0])
    learner = learning.adapter_for(FACTORY, str(learning.workspace())).learner
    learner.parser = GeminiParser(model, api_key=credential)
    opener = MagicMock()
    success = MagicMock()
    success.__enter__.return_value.read.return_value = json.dumps({"candidates": [
        {"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"signals": [signal(STATEMENT)]})}]}}
    ]}).encode()
    retry_info = {"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                        "retryDelay": "42.1s"}]}}
    opener.open.side_effect = [
        HTTPError("https://generativelanguage.googleapis.com/", 429, "synthetic provider error",
                  {"Retry-After": "10"}, BytesIO(json.dumps(retry_info).encode())),
        success,
    ]
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.parser.request.build_opener",
                        lambda *_: opener)

    original = note(api, STATEMENT, "rate-limited-note")
    assert original["learning"]["status"] == "processing_pending"
    waiting = api.post(f"{API}/retry").json()
    detail = waiting["events"][0]["update"]["issues"][-1]
    assert waiting["pending_count"] == 1 and detail["code"] == "model_rate_limited"
    assert opener.open.call_count == 1
    run = asyncio.run(run_portfolio_agent("Calculate for a synthetic client", client_id=CLIENT))
    assert run["result"]["error"]["code"] == "MODEL_RATE_LIMITED"
    assert run["result"]["error"]["retry_at"] == detail["retry_at"]
    assert run["result"]["error"]["retryable"]

    now[0] += 43
    completed = api.post(f"{API}/retry").json()
    assert opener.open.call_count == 2
    assert completed["pending_count"] == 0
    assert completed["context"]["team_declared"][0]["source_event_id"] == "rate-limited-note"


def test_portfolio_agent_receives_and_loses_saved_context_after_retraction(api, monkeypatch):
    from self_improving_ai.agents.portfolio.service import run_portfolio_agent

    first_turn = []

    class InspectingModel(ScriptedModel):
        async def generate_content_async(self, llm_request, stream=False):
            if not any(part.function_response for content in llm_request.contents
                       for part in content.parts or []):
                first_turn.append(STATEMENT in llm_request.config.system_instruction)
            async for response in super().generate_content_async(llm_request, stream):
                yield response

    async def scripted_run(message, **kwargs):
        model = InspectingModel(arguments={"client_id": CLIENT, "target_stock_weight": .6})
        return await run_portfolio_agent(message, model=model, **kwargs)

    monkeypatch.setattr(application, "run_portfolio_agent", scripted_run)
    runs = []
    for i in range(3):
        run = api.post("/api/portfolio/agent", json={
            "client_id": CLIENT, "message": "Compare the current allocation with 60 percent stocks.",
            "observation": {"event_id": f"comparison-{i}"}
        }).json()
        assert run["status"] == "success"
        runs.append(next(x["result"] for x in run["trace"] if x["kind"] == "result"))
        if i == 0:
            note(api, STATEMENT, "comparison-statement")
        elif i == 1:
            api.post(f"{API}/retract/comparison-statement").raise_for_status()
    assert first_turn == [False, True, False]
    assert [r.pop("personalized_analysis")["focus"] for r in runs] == ["compare", "downside", "compare"]
    assert runs[0] == runs[1] == runs[2]


def test_ordinary_use_calibration_and_personalized_scenarios(api):
    def context():
        return api.get(API).json()["context"]
    def adjust(chosen):
        ctx = context()
        payload = {"score":chosen, "revision":ctx["working_risk_index"]["calibration"]["revision"],
                   "profile_version":ctx["profile_version"]}
        api.post(API+"/risk-calibration", json=payload).raise_for_status()
        assert api.post(API+"/risk-calibration", json=payload).status_code == 409
    def compute(task, name, weight=None):
        payload = {"client_id":CLIENT, "task":task, "observation":{"event_id":name,"evidence_group_id":name}}
        if weight is not None:
            payload.update(target_stock_weight=weight)
            payload["observation"]["parameter_sources"] = {"target_stock_weight":"manual"}
        return api.post("/api/portfolio/execute", json=payload).json()["result"]
    assert context()["working_risk_index"]["effective"] == 50
    compute("risk", "plain")
    assert context()["working_risk_index"]["status"] == "insufficient"
    compute("risk", "lower", .60)
    before = context()["working_risk_index"]["automatic"]
    assert 48 < before < 48.1
    result = compute("holdings","positions")["personalized_analysis"]
    assert result["focus"] == "compare"
    assert result["scenario_step_pp"] == 5 and result["rows"][2]["stock_fall_usd"] == -16142.5
    assert result["rows"][2]["downside_change_usd"] == 1467.5
    adjust(72)
    assert context()["working_risk_index"]["effective"] == 72
    higher=compute("allocation","higher",.95)["personalized_analysis"]
    assert higher["score_used"] == 72 and higher["rows"][-1]["stock_weight"] == 1
    changed=context()["working_risk_index"]
    assert changed["effective"] == pytest.approx(72 + changed["automatic"] - before)
    assert changed["effective"] > 72
    assert api.get("/api/portfolio/learning/CLI-002").json()["context"]["working_risk_index"]["effective"] == 50
    adjust(None)
    restored=context()["working_risk_index"]
    assert restored["effective"] == restored["automatic"]


def test_ordinary_chat_targets_need_verified_request_and_execution(api, monkeypatch):
    message = "帮我把组合调到60/40，算一下差额。"
    extracted = signal(message, kind="attention", dimension="attention", value="other",
                       risk_basis="stock_target_inquiry", stock_target_pct=60)
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.adapter.PortfolioParser.parse_scoped",
                        lambda self, text, scope: [extracted])
    async def scripted(message, **kwargs):
        return await run_portfolio_agent(message, model=ScriptedModel(arguments={"client_id":CLIENT,
                                         "target_stock_weight":.6}), **kwargs)
    monkeypatch.setattr(application, "run_portfolio_agent", scripted)
    data=api.post("/api/portfolio/agent",json={"client_id":CLIENT,"message":message,
                  "observation":{"event_id":"chat","text_source":"manual"}}).json()
    assert data["status"] == "success"
    view=api.get(API).json()
    assert 48 < view["context"]["working_risk_index"]["automatic"] < 48.1
    event=view["events"][-1]["event"]
    for changes in ({"text_source":"default"},{"execution_trace":[]},{"user_text":"只查持仓"}):
        result=extract_signals({**event,**changes},lambda _: [extracted])
        assert not any(s["dimension"]=="risk_tilt" for s in result["signals"])


def test_directed_inquiry_with_second_topic_and_unusable_clause():
    message="能不能找一个波动小一些的方案？另有学费要付。"
    event=learning.event_from(CLIENT,"advisor_request",{},{"text_source":"manual"},message,"agent")
    request=signal("能不能找一个波动小一些的方案？",risk_basis="directed_inquiry")
    need=signal("另有学费要付。", kind="client_cue", dimension="attention",value="liquidity")
    result=extract_signals(event, lambda _: [request,need,{"evidence_id":99}])
    assert result["status"] == "ok"
    assert [(s["value"],s["strength"]) for s in result["signals"]] == [("conservative","weak"),("liquidity","weak")]
    assert any(i["code"]=="incomplete_coverage" for i in result["issues"])


@pytest.mark.parametrize("text,pct", [("改成六成股票",60),("股票六成五",65),("试算百分之七十五",75),
                        ("compare 60 percent stocks",60),("股债换成６０／４０",60),("债股比 40:60",60)])
def test_chat_target_verifies_numeric_wording(text,pct):
    from self_improving_ai.agents.portfolio.preference_learning.signals import mentioned_percentages
    assert mentioned_percentages(text) == {pct}


@pytest.mark.parametrize("text,excerpt,tool_targets,model_target,accepted", [
    ("股票40%，债券60%。","股票40%，债券60%。",[.6],60,False),
    ("股票40%，债券60%。","股票40%，债券60%。",[.4],40,True),
    ("只比较40/60和80/20。","只比较40/60和80/20。",[.4],40,False),
    ("先算40/60。另外比较80/20。","先算40/60。",[.4],40,False),
    ("请算60/40。","请算60/40。",[.6,.8],60,False),
    ("预期收益60%","预期收益60%",[.6],60,False),
    ("当前是80/20，想调到60/40。","当前是80/20，想调到60/40。",[.6],60,True),
    ("从80/20调到60/40。","从80/20调到60/40。",[.6],60,True),
])
def test_chat_target_is_uniquely_bound_to_stock(text, excerpt, tool_targets, model_target, accepted):
    from self_improving_ai.agents.portfolio.preference_learning.signals import verified_chat_target
    trace=[{"kind":"result","tool":"calculate_allocation_drift","result":{
        "status":"success","client_id":CLIENT,"target_source":"request_override",
        "asset_classes":{"stock":{"current_weight":.79,"target_weight":weight}}}} for weight in tool_targets]
    result=verified_chat_target({"client_id":CLIENT,"user_text":text,"execution_trace":trace},
                                {"stock_target_pct":model_target,"evidence_text":excerpt})
    assert result == ((.79,model_target/100) if accepted else None)


@pytest.mark.parametrize("wording,scope_quote,temporary",[
    ("请算调到60/40需要改多少。","",False),
    ("请算调到60/40需要改多少。",None,True),
    ("请算调到60/40，只限这次。","只限这次",True),
    ("请算调到60/40，本轮演练。","",True)])
def test_trial_scope_preserves_real_restrictions_and_account(wording,scope_quote,temporary):
    from self_improving_ai.agents.portfolio.preference_learning.signals import verified_trial_scope
    event={"user_text":wording,"scope":{"account_id":"A"},"evidence_group_id":"round"}
    item={"scope":{"account_id":"A","analysis_id":"round","applicability":"current"},"scope_quote":scope_quote}
    assert verified_trial_scope(event,item)==(item["scope"] if temporary else {"account_id":"A"})


def test_declared_willingness_anchors_the_score_even_in_same_analysis(api):
    group={"evidence_group_id":"same-analysis"}
    api.post("/api/portfolio/execute",json={"task":"risk","client_id":CLIENT,"target_stock_weight":.95,
             "observation":{**group,"parameter_sources":{"target_stock_weight":"manual"}}}).raise_for_status()
    def index():
        return api.get(API).json()["context"]["working_risk_index"]
    baseline=index()["automatic"]
    assert baseline>50
    api.post(API+"/notes",json={"message":STATEMENT,"observation":{**group,"event_id":"supplement"}}).raise_for_status()
    combined=index()
    assert combined["automatic"]==baseline
    assert combined["reference"]==pytest.approx(baseline - 25)
    api.post(API+"/retract/supplement").raise_for_status()
    assert index()["effective"]==baseline


def test_uncovered_compound_clause_is_retained_for_review():
    from self_improving_ai.agents.portfolio.preference_learning.parser import ParsePacket
    from self_improving_ai.agents.portfolio.preference_learning.prompts import PROMPT_VERSION
    text="查一下波动，另外需要为教育支出预留现金。"
    event=learning.event_from(CLIENT,"advisor_request",{},{"text_source":"manual"},text,"agent")
    first=signal(text,kind="attention",dimension="attention",value="other",evidence_id=1)
    second={**first,"evidence_id":2,"client_topic":"liquidity","reason":"实际支出需要现金"}
    def parse(rows):
        return extract_signals(event,lambda _: ParsePacket(json.dumps(rows),"scripted",prompt_version=PROMPT_VERSION))
    missing=parse([first])
    assert any(i["code"]=="incomplete_coverage" for i in missing["issues"])
    assert any(s["evidence_text"]=="另外需要为教育支出预留现金。" for s in missing["signals"] if s["kind"]=="uninterpreted_excerpt")
    complete=parse([first,second])
    assert not complete["issues"]
    assert any(s["kind"]=="client_cue" and s["value"]=="liquidity" for s in complete["signals"])


def test_mixed_trials_fixed_adaptive_and_retraction_in_normal_use(api):
    def index():
        return api.get(API).json()["context"]["working_risk_index"]
    def trial(event, group, target):
        api.post("/api/portfolio/execute", json={"task":"allocation", "client_id":CLIENT,
                 "target_stock_weight":target, "observation":{"event_id":event,"evidence_group_id":group,
                 "parameter_sources":{"target_stock_weight":"manual"}}}).raise_for_status()
        return index()
    def save(score, mode):
        ctx = api.get(API).json()["context"]
        api.post(API+"/risk-calibration", json={"score":score,"mode":mode,
                 "profile_version":ctx["profile_version"],"revision":ctx["working_risk_index"]["calibration"]["revision"]}).raise_for_status()
    assert trial("lower","one",.30)["effective"] < 50
    assert trial("higher","one",.95)["effective"] > 50
    latest = trial("lower-again","one",.30)
    assert latest["effective"] < 50 and latest["groups"] == 1
    assert trial("repeat","one",.30)["effective"] == latest["effective"]
    note(api, STATEMENT, "willingness")
    assert index()["effective"] == pytest.approx(latest["effective"] - 25)
    save(70,"fixed")
    after = trial("new","two",.95)
    assert 24 <= after["reference"] < 25 and after["effective"] == 70
    run = api.post("/api/portfolio/execute",json={"task":"holdings","client_id":CLIENT}).json()
    assert run["result"]["personalized_analysis"]["score_used"] == 70
    save(70,"adaptive")
    lower = trial("late","three",.20)
    assert 68 <= lower["effective"] < 70
    api.post(API+"/retract/late").raise_for_status()
    assert index()["effective"] == pytest.approx(70)
    save(None,"adaptive")
    assert index()["effective"] == pytest.approx(after["reference"])


def test_trial_influence_saturates_and_defaults_are_weaker():
    from self_improving_ai.agents.portfolio.analysis_focus import evidence_estimate
    def trials(count,source,target):
        return [{"group":str(i),"value":"growth","rule":source+"_raise_equity",
                 "parameters":{"target_stock_weight":target,"current_stock_weight":.5}} for i in range(count)]
    for count in (1,10,100):
        assert 50 < evidence_estimate(trials(count,"manual",1),None)["score"] <= 58
    assert 50 < evidence_estimate(trials(1,"default",1),None)["score"] <= 50.25
    assert 50 < evidence_estimate(trials(1,"manual",.51),None)["score"] <= 50.11


def test_pending_screen_snapshot_is_client_specific_and_never_fed_to_agent(api,monkeypatch):
    note(api,STATEMENT,"declaration")
    assert api.get(API).json()["context"]["working_risk_index"]["effective"] == 25
    learner=learning.adapter_for(FACTORY,str(learning.workspace())).learner
    learner.parser=None
    note(api,"a second statement","pending")
    view=api.get(API).json()
    assert view["context"] is None and view["last_confirmed_display"]["score"] == 25
    assert learning.read_analysis_context(CLIENT)["context"] is None
    assert "last_confirmed_display" not in api.get("/api/portfolio/learning/CLI-002").json()


def test_upgrade_of_existing_calibration_preserves_effective_score(api):
    trial=calculate(api,"default")
    assert trial["status"]=="success"
    ctx=api.get(API).json()["context"]
    old_reference=ctx["risk_preference_scores"][0]["score"]
    with learning.Journal().db() as db:
        db.execute("INSERT INTO risk_calibrations(client,revision,chosen,baseline,created,mode) "
                   "VALUES(?,?,?,?,?,NULL)",(CLIENT,1,65,old_reference-2,"legacy"))
    for _ in range(2):
        index=api.get(API).json()["context"]["working_risk_index"]
        assert index["effective"] == pytest.approx(67) and index["calibration"]["mode"] == "adaptive"


def test_work_day_limit_reopens_after_local_midnight(api, monkeypatch):
    from datetime import datetime, timedelta, timezone
    clock = datetime(2026,11,28,0,0,tzinfo=timezone.utc)
    monkeypatch.setattr(learning, "now", lambda: clock.isoformat())

    def trial(event, group, target):
        nonlocal clock
        clock += timedelta(seconds=1)
        api.post("/api/portfolio/execute",json={"task":"allocation","client_id":CLIENT,"target_stock_weight":target,
                 "observation":{"event_id":event,"evidence_group_id":group,
                                "parameter_sources":{"target_stock_weight":"manual"}}}).raise_for_status()
        return api.get(API).json()["context"]["working_risk_index"]

    first = trial("lower-first","round-a",.2)
    same_round = trial("lower-repeat","round-a",.2)
    new_round = trial("lower-second","round-b",.2)
    saturated = trial("lower-third","round-c",.2)
    assert first["effective"] == same_round["effective"] > new_round["effective"] == saturated["effective"]
    assert 47 <= new_round["effective"] < 48
    rising = trial("raise","round-c",1)
    falling = trial("lower-fourth","round-c",.2)
    assert rising["effective"] > falling["effective"] == saturated["effective"]
    # 05:00 UTC is midnight in New York on this date.
    clock = datetime(2026,11,28,5,0,tzinfo=timezone.utc)
    tomorrow = trial("lower-next-day","round-c",.2)
    assert tomorrow["effective"] < saturated["effective"]


def test_grade_of_declared_risk_and_mixed_objectives(api, monkeypatch):
    messages = {
        "客户有点不想冒太大风险": [("behavioral_hint","conservative","directed_inquiry")],
        "客户有点保守": [("explicit_statement","conservative","declared_attitude")],
        "客户十分保守": [("explicit_statement","conservative","declared_attitude")],
        "客户希望进一步提高风险敞口，但是又很担心本金": [
            ("explicit_statement","growth","declared_attitude"),("explicit_statement","conservative","declared_attitude")],
        "客户明确偏进取，但也明确拒绝承担波动": [
            ("explicit_statement","growth","declared_attitude"),("explicit_statement","conservative","declared_attitude")],
    }
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.adapter.PortfolioParser.parse_scoped",
                        lambda _, text, scope: [signal(text,kind=kind,value=value,risk_basis=basis)
                                               for kind,value,basis in messages[text]])
    def submit(i, message):
        note(api,message,f"risk-language-{i}")
        return api.get(API).json()["context"]["working_risk_index"]
    one, two, strong, mixed, conflict = [submit(i, text) for i, text in enumerate(messages)]
    assert (one["effective"],two["effective"],strong["effective"]) == (40,40,15)
    assert mixed["status"] == "scored" and 15 < mixed["effective"] < 17
    assert mixed["breakdown"]["anchor"] == 15
    assert conflict["status"] == "conflict" and conflict["effective"] == mixed["effective"]


def test_same_round_semantics_reused_until_explicit_reinterpretation(tmp_path):
    from self_improving_ai.agents.portfolio.preference_learning.parser import ParsePacket
    from self_improving_ai.agents.portfolio.preference_learning.prompts import PROMPT_VERSION
    count = 0
    def parser(text):
        nonlocal count
        count += 1
        return ParsePacket(json.dumps([signal(text)]), "gemini_live", "synthetic", PROMPT_VERSION)
    learner = PortfolioLearner(tmp_path, parser=parser)
    first = learning.event_from(CLIENT,"client_statement",{},{"event_id":"original","evidence_group_id":"shared"},
                                STATEMENT,"advisor_note")
    first.update(outcome="success",revision=2)
    repeat = {**first,"event_id":"repeat","request_id":"repeat"}
    learner.process_event(first)
    second = learner.process_event(repeat)
    assert second["extraction"]["parser"]["reused_from_event"] == "original" and count == 1
    learner.process_event({**repeat,"revision":3})
    assert count == 2


def test_notes_distinguish_objectives_reactions_and_short_term_risk(api, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo
    from self_improving_ai.agents.portfolio.risk_scoring import risk_index
    from self_improving_ai.agents.portfolio.preference_learning.parser import ParsePacket
    from self_improving_ai.agents.portfolio.preference_learning.prompts import PROMPT_VERSION

    stable = "客户整体有点保守"
    returns = "客户愿意把提高收益的优先级放高"
    loss = "客户亏损了一些钱，吓坏了"
    principal = "客户吓坏了，现在只期望保住本金"
    recovery = "客户眼红别人赚钱，现在又敢了"
    other = "The other client is very aggressive."
    topics = {stable: [signal(stable)], returns: [signal(returns,kind="behavioral_hint",value="growth",
              risk_basis="allocation_intent")], loss: [signal(loss,kind="client_cue",dimension="attention",value="loss_reaction")],
              principal: [signal(principal)], recovery: [signal(recovery,kind="behavioral_hint",value="growth",
              risk_basis="current_risk_attitude")]}
    clock = datetime.now(timezone.utc) - timedelta(minutes=1)
    monkeypatch.setattr(learning,"now", lambda: clock.isoformat())
    history_used = []
    def parse(self, text, scope):
        history_used.append((text,list(self.prior_notes)))
        signals = [signal(other,value="growth")] if text == other else topics[text]
        return ParsePacket(json.dumps(signals),"gemini_live","fixture",PROMPT_VERSION)
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.adapter.PortfolioParser.parse_scoped",parse)
    api.post("/api/portfolio/learning/CLI-002/notes",json={"message":other}).raise_for_status()
    estimates = []
    for i,text in enumerate(topics):
        clock += timedelta(seconds=1)
        note(api,text,f"note-context-{i}")
        estimates.append(api.get(API).json()["context"]["working_risk_index"])
    assert [s["effective"] for s in estimates] == [40,40,40,33,43]
    assert estimates[-1]["breakdown"]["anchor"] == 40
    assert principal in history_used[-1][1] and recovery not in history_used[-1][1]
    assert all(other not in notes for _, notes in history_used)
    learner = learning.adapter_for(FACTORY,str(learning.workspace())).learner
    observations = learner.get_context(CLIENT, include_risk_observations=True)["risk_observations"]
    work_date = clock.astimezone(ZoneInfo(settings.portfolio_work_timezone)).date()
    assert risk_index(observations, {}, work_date=work_date+timedelta(days=7))["effective"] == 40
    api.post(API+"/retract/note-context-4").raise_for_status()
    assert api.get(API).json()["context"]["working_risk_index"]["effective"] == 33


def test_partial_reinterpretation_retains_last_usable_risk_evidence(api, monkeypatch):
    from self_improving_ai.agents.portfolio.preference_learning.parser import ParsePacket
    from self_improving_ai.agents.portfolio.preference_learning.prompts import PROMPT_VERSION
    note(api, STATEMENT, "partial-reread")
    assert api.get(API).json()["context"]["working_risk_index"]["effective"] == 25
    invalid = [signal(STATEMENT,risk_basis="behavioral_hint/risk_tilt"),
               signal(STATEMENT,kind="client_cue",dimension="attention",value="risk_change")]
    learner = learning.adapter_for(FACTORY,str(learning.workspace())).learner
    learner.parser = lambda _: ParsePacket(json.dumps(invalid),"gemini_live","fixture",PROMPT_VERSION)
    api.post(API+"/reinterpret/partial-reread",json={"revision":2}).raise_for_status()
    view = api.get(API).json()
    assert view["events"][0]["delivery_status"] == "needs_review"
    assert view["context"]["working_risk_index"]["effective"] == 25


def test_clear_removes_all_learning_for_one_client_and_guards_stale_requests(api, monkeypatch):
    from self_improving_ai.agents.portfolio.application import request_slot

    note(api, STATEMENT, "keep")
    note(api, STATEMENT, "remove")
    api.post(API+"/retract/remove").raise_for_status()
    other = "/api/portfolio/learning/CLI-002"
    note(api, STATEMENT, "other-client")
    api.post(other+"/notes", json={"message":STATEMENT, "observation":{"event_id":"other-client"}}).raise_for_status()
    ctx = api.get(API).json()["context"]
    calibration = {"score":70,"mode":"fixed","profile_version":ctx["profile_version"],"revision":0}
    api.post(API+"/risk-calibration", json=calibration).raise_for_status()
    learner=learning.adapter_for(FACTORY,str(learning.workspace())).learner
    monkeypatch.setattr(learner,"parser",None)
    note(api,"still pending","pending")
    assert api.get(API).json()["pending_count"] == 1
    old = learning.event_from(CLIENT, "holdings", {}, {"event_id":"late-result"})
    journal, cached = learning.record_start(old)
    assert cached is None
    assert api.post(API+"/clear", json={"confirm":False,"generation":0}).status_code == 422
    with request_slot(CLIENT, "running"):
        assert api.post(API+"/clear", json={"confirm":True,"generation":0}).status_code == 409
    assert api.get(API).json()["total_events"] > 0
    api.post(API+"/clear", json={"confirm":True,"generation":0}).raise_for_status()
    run = learning.record_finish(journal,old,{"run_id":"late","status":"success","result":{}})
    assert run["learning"]["status"] == "cleared"
    cleared = api.get(API+"/export").json()
    assert cleared["view"]["generation"] == 1 and cleared["deliveries"] == []
    assert cleared["view"]["context"]["working_risk_index"]["effective"] == 50
    assert cleared["view"]["context"]["working_risk_index"]["calibration"]["chosen"] is None
    assert learner.export_client(CLIENT)["evidence_history"] == {"extractions":[],"retracted_event_ids":[]}
    with journal.db() as db:
        assert all(db.execute(f"SELECT count(*) FROM {table} WHERE client=?", (CLIENT,)).fetchone()[0] == 0
                   for table in ("requests", "risk_calibrations"))
    assert api.get(other).json()["context"]["working_risk_index"]["effective"] == 25
    assert api.post(API+"/risk-calibration",json={**calibration,"profile_version":0}).status_code == 409
    api.post(API+"/risk-calibration",json={**calibration,"profile_version":0,"generation":1}).raise_for_status()
    calculate(api,"new")
    assert api.post(API+"/clear", json={"confirm":True,"generation":0}).status_code == 409
    assert api.get(API).json()["total_events"] == 1


def test_paged_operations_report_own_score_changes_and_withdrawal(api):
    note(api, STATEMENT, "oldest-note")
    ctx = api.get(API).json()["context"]
    api.post(API+"/risk-calibration",json={"score":70,"mode":"fixed","profile_version":ctx["profile_version"],
             "revision":0}).raise_for_status()
    for name in ("first-trial","repeat-trial"):
        api.post("/api/portfolio/execute",json={"task":"allocation","client_id":CLIENT,
                 "target_stock_weight":.6,"observation":{"event_id":name,"evidence_group_id":"shared",
                 "parameter_sources":{"target_stock_weight":"manual"}}}).raise_for_status()
    for i in range(9):
        api.post("/api/portfolio/execute",json={"task":"holdings","client_id":CLIENT,
                 "observation":{"event_id":f"holdings-{i}"}}).raise_for_status()
    api.post(API+"/retract/oldest-note").raise_for_status()
    first = api.get(API).json()
    second = api.get(API+"?page=2").json()
    assert first["pagination"] == {"page":1,"pages":2,"page_size":10}
    assert second["pagination"]["page"] == 2 and second["total_events"] == 12
    assert first["latest_event"] == second["latest_event"]
    assert len(first["events"]) == 10
    cards = {(row["event_id"],row["kind"]):row for row in second["events"]}
    initial = cards["oldest-note","event"]["score_effects"][-1]["changes"][0]
    withdrawn = cards["oldest-note","retract"]["score_effects"][-1]["changes"][0]
    trial = cards["first-trial","event"]["score_effects"][-1]["changes"][0]
    assert initial["effective"] == [50,25] and initial["components"] == {"anchor":-25}
    assert withdrawn["effective"] == [70,70] and withdrawn["components"] == {"anchor":25}
    assert withdrawn["reference"][1] - withdrawn["reference"][0] == pytest.approx(25)
    assert trial["effective"] == [70,70] and trial["components"]["trial_points"] < 0
    duplicate = next(row for row in first["events"] if row["event_id"]=="repeat-trial")
    effect = duplicate["score_effects"][-1]["changes"][0]
    assert effect["reference"][0] == effect["reference"][1] and not effect["components"]
    assert api.get(API+"?page=20").json()["pagination"]["page"] == 2


@pytest.mark.asyncio
async def test_agent_tool_cannot_use_household_total_instead_of_explicit_account():
    from self_improving_ai.agents.portfolio.service import run_portfolio_agent

    result = await run_portfolio_agent("Compare the selected account", client_id=CLIENT,
                                       account_id="ACC-001A", model=ScriptedModel())
    assert result["status"] == "success"
    reply = next(x["result"] for x in result["trace"] if x["kind"] == "result")
    assert reply["error"]["code"] == "ACCOUNT_CONTEXT_MISMATCH"


def test_many_independent_default_trials_have_bounded_support(tmp_path):
    learner = create_adapter(workspace_dir=tmp_path).learner
    learner.parser = lambda message: [signal(message)]
    event = learning.event_from(CLIENT, "client_statement", {}, {"event_id": "statement"}, STATEMENT)
    learner.process_event(event)
    for i in range(30):
        trial = learning.event_from(CLIENT, "allocation_drift", {}, {"event_id": f"trial-{i}"})
        trial.update(outcome="success", effective_parameters={"target_stock_weight": .95},
                     parameter_sources={"target_stock_weight": "default"},
                     prior_context={"current_stock_weight": .8})
        result = learner.process_event(trial)
    risk = result["state"]["beliefs"]["risk_tilt"]
    assert risk["probabilities"]["conservative"] > risk["probabilities"]["growth"]
    assert len(risk["active_signal_ids"]) == 6
    assert len([i for i in result["ignored"] if i["reason_code"] == "default_evidence_limit"]) == 25
    assert len(result["state"]["attention"]["allocation_scenarios"]) == 30


def test_cues_reinterpretation_and_risk_result_use(api, monkeypatch):
    text = "We need cash before the next tuition payment."
    note(api, text, "tuition-note")
    first = api.get(API).json()["context"]
    assert first["client_cues"] == []
    extracted = signal(text, kind="client_cue", dimension="attention", value="liquidity")
    extracted.pop("evidence_text")
    extracted["evidence_id"] = 1
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.adapter.PortfolioParser.parse_scoped",
                        lambda self, message, scope: [extracted])
    for _ in range(2):
        api.post(f"{API}/reinterpret/tuition-note", json={"revision": 2}).raise_for_status()
    view = api.get(API).json()
    assert len(view["context"]["client_cues"]) == 1
    assert view["context"]["team_declared"] == view["context"]["tentative_preferences"] == []
    assert view["events"][0]["revision"] == 3
    assert api.get("/api/portfolio/learning/CLI-002").json()["context"]["client_cues"] == []
    run = api.post("/api/portfolio/execute", json={"task":"risk","client_id":CLIENT}).json()
    assert run["learning_context_used"]["context"]["client_cues"][0]["evidence_text"] == text
    attention = api.get(API).json()["context"]["recent_attention"]
    risk = next(x for x in attention if x["kind"] == "calculation_topic")
    assert risk["basis"]["annual_volatility"] == run["result"]["annual_volatility"]
    assert api.post(f"{API}/retract/tuition-note").json()["context"]["client_cues"] == []
    assert api.post(f"{API}/reinterpret/tuition-note",json={"revision":3}).status_code == 409


def test_weak_behavior_in_round_is_tentative_and_retractable(api, monkeypatch):
    text = "They have been trying smaller steps this time."
    hint = signal(text,kind="behavioral_hint",dimension="adjustment_style",value="gradual",
                  scope={"applicability":"current"})
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.adapter.PortfolioParser.parse_scoped",
                        lambda self, message, scope: [hint])
    api.post(f"{API}/notes",json={"message":text,"observation":{"event_id":"pace","evidence_group_id":"round-a"}}).raise_for_status()
    headers={"X-Portfolio-Analysis":"round-a"}
    view=api.get(API,headers=headers).json()["context"]
    assert view["team_declared"] == []
    belief=view["tentative_preferences"][0]
    assert .5 < belief["probabilities"]["gradual"] < .6
    assert belief["sources"][0]["quote"] == text
    assert api.get(API,headers={"X-Portfolio-Analysis":"round-b"}).json()["context"]["tentative_preferences"] == []
    api.post(f"{API}/retract/pace").raise_for_status()
    assert api.get(API,headers=headers).json()["context"]["tentative_preferences"] == []


def test_invalid_source_reference_cannot_be_used():
    event=learning.event_from(CLIENT,"client_statement",{},{},STATEMENT,"advisor_note")
    invalid={**signal(STATEMENT),"evidence_id":99}
    result=extract_signals(event, lambda _: [invalid])
    assert result["status"] == "partial" and result["signals"] == []


def test_risk_score_audit_and_scenario_comparison(api):
    def score():
        return api.get(API).json()["context"]["risk_preference_scores"][0]
    assert score()["score"] is None
    note(api, STATEMENT, "declared-risk")
    card = score()
    assert card["score"] == pytest.approx(100 / 9.5)
    assert card["evidence"][0]["multipliers"] == [8, 1, .5]
    assert card["evidence"][0]["running_score"] == pytest.approx(card["score"])
    assert card["evidence"][0]["source"] == STATEMENT
    assert card["evidence"][0]["criterion"] == "declared_attitude"
    for target in (0, .6, 1):
        run = api.post("/api/portfolio/execute", json={"task":"risk","client_id":CLIENT,
                       "target_stock_weight":target}).json()
        comp = run["result"]["scenario_comparison"]
        assert comp["source"] == "manual_scenario" and comp["weights"]["stock"] == target
        assert comp["volatility_delta_pp"] == pytest.approx(
            100 * (comp["annual_volatility"] - run["result"]["annual_volatility"]))
        assert run["learning_context_used"]["context"]["risk_preference_scores"][0]["score"] == card["score"]
        assert score()["score"] == card["score"]
    assert api.get("/api/portfolio/learning/CLI-002").json()["context"]["risk_preference_scores"][0]["score"] is None
    api.post(f"{API}/retract/declared-risk").raise_for_status()
    assert score()["score"] is None


@pytest.mark.parametrize("criterion,assertion,expected", [
    ("no_direction","affirmed",None), ("observed_risk_choice","affirmed","weak"),
    ("risk_return_tradeoff","affirmed","explicit"), ("risk_return_tradeoff","hypothetical",None),
    ("declared_attitude","negated",None),
])
def test_semantic_evidence_gate(criterion, assertion, expected):
    event = learning.event_from(CLIENT, "client_statement", {}, {}, STATEMENT, "advisor_note")
    evidence = signal(STATEMENT,risk_basis=criterion,assertion=assertion)
    result = extract_signals(event, lambda _: [evidence])
    scored = [s for s in result["signals"] if s["dimension"] == "risk_tilt" and s["strength"]!="none"]
    assert [s["strength"] for s in scored] == ([expected] if expected else [])


def test_conflicting_risk_evidence_withholds_score_until_retracted(api, monkeypatch):
    text = "Advisor denies the earlier conservative risk statement."
    note(api, STATEMENT, "initial-declaration")
    monkeypatch.setattr("self_improving_ai.agents.portfolio.preference_learning.adapter.PortfolioParser.parse_scoped",
                        lambda self, message, scope: [signal(message, assertion="negated")])
    note(api, text, "later-denial")
    card=api.get(API).json()["context"]["risk_preference_scores"][0]
    assert card["status"] == "conflict" and card["score"] is None
    restored=api.post(f"{API}/retract/later-denial").json()["context"]["risk_preference_scores"][0]
    assert restored["score"] == pytest.approx(100 / 9.5)
    assert len(restored["evidence"]) == 1
