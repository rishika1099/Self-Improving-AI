"""Component A endpoints; explicit calculations and model-driven requests are separate."""
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from self_improving_ai.agents.portfolio.service import execute_task, run_portfolio_agent
from self_improving_ai.agents.portfolio.application import calculate_for_client, analyze_for_client, save_client_note, require_client
from self_improving_ai.agents.portfolio.learning import Journal, analysis_scope

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


class Observation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    event_id: str | None = Field(default=None, min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    evidence_group_id: str | None = Field(default=None,min_length=1,max_length=100,pattern=r'^[A-Za-z0-9_-]+$')
    parameter_sources: dict[str, Literal['default','manual','unknown']] = Field(default_factory=dict,max_length=20)
    text_source: Literal['default','manual','unknown'] = 'unknown'


class CalculationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task: Literal["holdings", "scenarios", "allocation", "risk", "projection", "quote", "history"]
    client_id: str = Field(default="CLI-001", max_length=30)
    account_id: str = Field(default="", max_length=50)
    symbol: str = Field(default="AAPL", max_length=20)
    start_date: str = ""
    end_date: str = ""
    num_simulations: int = Field(default=5000, ge=1, le=100000)
    seed: int = Field(default=42, ge=0, le=4294967295)
    target_stock_weight: float | None = Field(default=None, ge=0, le=1)
    target_amount: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    years: int | None = Field(default=None, ge=1, le=120)
    annual_contribution: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    annual_withdrawal: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    observation: Observation = Field(default_factory=Observation)

    @model_validator(mode="after")
    def reject_ignored_scenarios(self):
        if self.task != "allocation" and self.target_stock_weight is not None:
            raise ValueError("Stock target is only supported for allocation drift.")
        if self.task != "projection" and any(getattr(self, key) is not None for key in
                ("target_amount", "years", "annual_contribution", "annual_withdrawal")):
            raise ValueError("Goal overrides are only supported for goal projection.")
        if self.account_id and self.task not in {"holdings", "allocation", "risk"}:
            raise ValueError("This task does not support an account filter.")
        return self


class AgentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=3000)
    client_id: str | None = Field(default=None,min_length=1,max_length=30)
    account_id: str | None = Field(default=None, min_length=1, max_length=50)
    observation: Observation = Field(default_factory=Observation)


@router.post("/execute")
async def calculate(request: CalculationRequest):
    args = {"client_id": request.client_id}
    if request.task in {"holdings", "allocation", "risk"}:
        args["account_id"] = request.account_id
    if request.task == "projection":
        args.update(num_simulations=request.num_simulations, seed=request.seed)
        args.update({key: getattr(request, key) for key in
                     ("target_amount", "years", "annual_contribution", "annual_withdrawal")
                     if getattr(request, key) is not None})
    if request.task == "allocation" and request.target_stock_weight is not None:
        args["target_stock_weight"] = request.target_stock_weight
    if request.task in {"quote", "history"}:
        args = {"symbol": request.symbol}
    if request.task == "history":
        args.update(start_date=request.start_date, end_date=request.end_date)
    if 'client_id' not in request.model_fields_set:
        # Legacy callers may compute with their old default, but cannot silently
        # attach learning records to that default household.
        result = await run_in_threadpool(execute_task, request.task, args)
        result['learning'] = {'status':'client_not_selected'}
        return result
    try:
        metadata=request.observation.model_dump()
        metadata['requested_keys']=[key for key in args if key in request.model_fields_set]
        return await run_in_threadpool(calculate_for_client,request.task,args,
                                      request.client_id,metadata)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.post("/agent")
async def ask_agent(request: AgentRequest):
    if not request.client_id:
        if request.account_id:
            raise HTTPException(422, 'Select a client before specifying an account.')
        result = await run_portfolio_agent(request.message)
        result['learning'] = {'status':'client_not_selected'}
        return result
    try:
        return await analyze_for_client(request.message, request.client_id,
                                        request.observation.model_dump(), account_id=request.account_id)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.get('/learning/{client_id}')
def learning_view(client_id: str, x_portfolio_analysis: str | None = Header(default=None, max_length=100)):
    try:
        require_client(client_id)
        return Journal().view(client_id, analysis_scope(group=x_portfolio_analysis))
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503,'Customer learning storage is unavailable.') from exc


@router.post('/learning/{client_id}/notes')
def learning_note(client_id: str, request: AgentRequest):
    if request.client_id and request.client_id != client_id:
        raise HTTPException(409,'Client context mismatch.')
    try:
        return save_client_note(request.message, client_id, request.observation.model_dump(),
                                account_id=request.account_id)
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc


@router.post('/learning/{client_id}/retry')
def learning_retry(client_id: str):
    try:
        require_client(client_id)
        journal=Journal()
        status=journal.sync(client_id, retry_pending=True)
        return {**journal.view(client_id),'last_delivery_status':status}
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503,'Customer learning storage is unavailable.') from exc


class ReinterpretRequest(BaseModel):
    revision: int = Field(ge=1)


@router.post('/learning/{client_id}/reinterpret/{event_id}')
def reinterpret_record(client_id: str, event_id: str, request: ReinterpretRequest):
    try:
        require_client(client_id)
        journal = Journal()
        journal.reinterpret(client_id, event_id, request.revision)
        return {"status": journal.sync(client_id)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/learning/{client_id}/retract/{event_id}')
def learning_retract(client_id: str,event_id: str):
    try:
        require_client(client_id)
        journal=Journal()
        journal.retract(client_id,event_id)
        status=journal.sync(client_id)
        return {**journal.view(client_id),'last_delivery_status':status}
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503,'Could not save the retraction; retry later.') from exc


@router.get('/learning/{client_id}/export')
def learning_export(client_id: str):
    try:
        require_client(client_id)
        journal=Journal()
        return {'schema_version':'portfolio-integration-export-v1',
                'view':journal.view(client_id),'deliveries':journal.rows(client_id)}
    except ValueError as exc:
        raise HTTPException(404,str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503,'Could not export customer records.') from exc
