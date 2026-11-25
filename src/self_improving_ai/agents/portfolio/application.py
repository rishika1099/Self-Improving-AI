"""Shared entry points for advisor-origin requests; C may call these with original provenance."""
import re
import asyncio
from contextlib import contextmanager
from threading import Lock
from self_improving_ai.tools.portfolio.holdings import _load_households
from self_improving_ai.agents.portfolio.learning import (
    event_from, read_analysis_context, record_finish, record_start, valid_client,
)
from self_improving_ai.agents.portfolio.service import execute_task, run_portfolio_agent

_inflight = set()
_request_lock = Lock()


@contextmanager
def request_slot(client_id,event_id):
    key=(client_id,event_id)
    with _request_lock:
        if key in _inflight:
            raise ValueError('This request is still running. Wait before retrying the same event.')
        _inflight.add(key)
    try:
        yield
    finally:
        with _request_lock:
            _inflight.discard(key)


def require_client(client_id):
    if not valid_client(client_id):
        raise ValueError('Select a known client before recording an operation.')


def require_matching_message(message, client_id, account_id=None):
    households = _load_households()
    if account_id and not any(
        account["account_id"] == account_id
        for row in households if row["client_id"] == client_id
        for account in row.get("accounts", [])
    ):
        raise ValueError("The specified account does not belong to the selected client.")
    for row in households:
        if row['client_id'] == client_id:
            if account_id:
                other_accounts = [a["account_id"] for a in row.get("accounts", [])
                                  if a["account_id"] != account_id]
                if any(identifier_in_message(label, message) for label in other_accounts):
                    raise ValueError("The message names an account other than the specified account.")
            continue
        labels=(row['client_id'],row['profile']['name'],*[a['account_id'] for a in row.get('accounts',[])])
        if any(identifier_in_message(label, message) for label in labels):
            raise ValueError('The message names another client. Select that client or clarify the message before saving.')


def identifier_in_message(identifier, message):
    return re.search(r'(?<![A-Za-z0-9_-])' + re.escape(identifier) + r'(?![A-Za-z0-9_-])',
                     message, re.IGNORECASE)


def calculate_for_client(task, arguments, client_id, metadata):
    require_client(client_id)
    event = event_from(client_id, 'allocation_drift' if task=='allocation' else task,arguments,metadata)
    with request_slot(client_id,event['event_id']):
        # Capture the previously verified profile before this calculation creates a pending event.
        context = (read_analysis_context(client_id, arguments.get("account_id") or None, metadata.get("evidence_group_id"))
                   if task == "risk" else None)
        journal, cached = record_start(event)
        if cached is not None:
            return cached
        run = execute_task(task,arguments)
        if context is not None:
            run["learning_context_used"] = context
        return record_finish(journal,event,run)


async def analyze_for_client(message, client_id, metadata, *, account_id=None):
    await asyncio.to_thread(require_client,client_id)
    await asyncio.to_thread(require_matching_message, message, client_id, account_id)
    event = event_from(client_id, 'advisor_request', {'account_id': account_id}, metadata, message, 'agent')
    with request_slot(client_id,event['event_id']):
        journal, cached = await asyncio.to_thread(record_start,event)
        if cached is not None:
            return cached
        # This lets a delivered learner use explicit current input in this request.
        if journal:
            await asyncio.to_thread(journal.sync,client_id)
        context = await asyncio.to_thread(read_analysis_context, client_id, account_id, metadata.get("evidence_group_id"))
        # A model parse can outlast the short web write budget. Give the active write
        # a bounded chance to finish before answering with an unavailable snapshot.
        deadline = asyncio.get_running_loop().time() + 5
        while context.get("status") == "module_busy" and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(.25)
            context = await asyncio.to_thread(read_analysis_context, client_id, account_id, metadata.get("evidence_group_id"))
        run = await run_portfolio_agent(message, client_id=client_id, learning_context=context,
                                        account_id=account_id)
        run['learning_context_used'] = context
        return await asyncio.to_thread(record_finish,journal,event,run)


def save_client_note(message, client_id, metadata, *, account_id=None):
    require_client(client_id)
    require_matching_message(message, client_id, account_id)
    event = event_from(client_id, 'client_statement', {'account_id': account_id}, metadata, message, 'advisor_note')
    with request_slot(client_id,event['event_id']):
        journal, cached = record_start(event)
        if cached is not None:
            return cached
        run = {'run_id':event['event_id'],'status':'success','mode':'client_note',
               'result':{'status':'success','message':'Advisor note recorded; parsing status is reported separately.'}}
        run=record_finish(journal,event,run)
    if run['learning']['status']=='storage_error':
        run['status']='error'
        run['result']={'status':'error','message':'The client note could not be saved. Retry later.'}
    return run
