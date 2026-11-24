"""Regression coverage for the audited delivery through the real Portfolio A API."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from self_improving_ai.agents.portfolio import application, learning
from self_improving_ai.agents.portfolio.preference_learning import PortfolioLearner
from self_improving_ai.agents.portfolio.preference_learning.adapter import create_adapter
from self_improving_ai.agents.portfolio.preference_learning.parser import GeminiParser
from self_improving_ai.dashboard.app import app
from tests.test_agents.test_portfolio_agent import ScriptedModel

CLIENT = "CLI-001"
API = f"/api/portfolio/learning/{CLIENT}"
STATEMENT = "The client is conservative."
ACCOUNT = "The education account should take less risk."
FACTORY = "self_improving_ai.agents.portfolio.preference_learning.adapter:create_adapter"


def signal(text, *, kind="explicit_statement", dimension="risk_tilt", value="conservative", **extra):
    return {"kind": kind, "dimension": dimension, "value": value, "assertion": "affirmed",
            "evidence_text": text, "reason": "Synthetic fixture", "scope": None, **extra}


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
    assert PortfolioLearner(learning.workspace() / "learner").get_context(CLIENT) == context
    assert api.post(f"{API}/retract/statement").json()["context"]["team_declared"] == []


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
    assert api.get(API).json()["context"] is None
    learner.parser = parser
    view = api.post(f"{API}/retry").json()
    assert view["pending_count"] == 0
    assert view["context"]["team_declared"][0]["value"] == "conservative"


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
    assert runs[0] == runs[1] == runs[2]


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
