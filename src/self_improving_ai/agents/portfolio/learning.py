"""Durable integration journal, NOT the teammate's preference learning algorithm.

Delivery is at-least-once. The adapter must deduplicate (client_id,event_id,revision)
and retractions. A failed delivery blocks later deliveries for that client.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache, wraps
from pathlib import Path
from threading import RLock, Lock, Thread, Event
from time import monotonic
from uuid import uuid4

from self_improving_ai.config import settings
from self_improving_ai.tools.portfolio.holdings import _load_households


def valid_client(client_id):
    return bool(client_id) and any(c['client_id'] == client_id for c in _load_households())


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def workspace():
    return Path(os.environ.get('SIA_PORTFOLIO_LEARNING_DIR',
                str(Path(settings.db_path).parent / 'portfolio-learning')))


@lru_cache(maxsize=8)
def adapter_for(spec, directory):
    if not spec:
        return None
    module, factory = spec.split(':', 1)
    return getattr(importlib.import_module(module), factory)(workspace_dir=Path(directory)/'learner')


_lock = RLock()
_adapter_gate = Lock()


def bounded_adapter_call(function, timeout=1.0):
    """Never hold the database lock while calling external module code.

    A timed-out daemon call keeps the gate until it finishes: no second call or
    context read can race an uncertain write. The durable outbox will retry the
    same idempotency key, so a late success cannot create a second contribution.
    """
    if not _adapter_gate.acquire(blocking=False):
        raise TimeoutError('Learning module is still busy.')
    done, result = Event(), {}
    def work():
        try:
            result['value'] = function()
        except BaseException as exc:
            result['error'] = exc
        finally:
            _adapter_gate.release()
            done.set()
    Thread(target=work,daemon=True).start()
    if not done.wait(timeout):
        raise TimeoutError('Learning module did not finish in time.')
    if 'error' in result:
        raise result['error']
    return result['value']


def checked_context(context, client, scope):
    if (not isinstance(context,dict) or context.get('client_id') != client
            or context.get('status') in {'error','pending','partial'}
            or context.get('scope') != scope):
        raise ValueError('Invalid client context.')
    if 'summary' in context and not isinstance(context['summary'],str):
        raise ValueError('Invalid context summary.')
    for key in ('team_declared','tentative_preferences','recent_attention','unknowns','usage_notes'):
        if key in context and (not isinstance(context[key],list)
                or any(not isinstance(x,(str,dict)) for x in context[key])):
            raise ValueError('Invalid context items.')
    encode(context)
    return context


def serialized(function):
    @wraps(function)
    def invoke(*args,**kwargs):
        with _lock:
            return function(*args,**kwargs)
    return invoke


class Journal:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else workspace()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'observations.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests(
                    client TEXT, event TEXT, intent TEXT, created TEXT, result TEXT,
                    PRIMARY KEY(client,event));
                CREATE TABLE IF NOT EXISTS deliveries(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT, event TEXT,
                    revision INTEGER, kind TEXT, payload TEXT, status TEXT DEFAULT 'pending',
                    response TEXT, UNIQUE(client,event,revision,kind));
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def begin(self, client, event_id, intent):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,NULL)',
                       (client,event_id,encode(intent),now()))
            row = db.execute('SELECT * FROM requests WHERE client=? AND event=?',
                             (client,event_id)).fetchone()
            if row['intent'] != encode(intent):
                raise ValueError('This event ID already belongs to different input.')
            return dict(row)

    @serialized
    def append(self, event):
        with self.db() as db:
            key = (event['client_id'],event['event_id'],event['revision'],'event')
            row = db.execute('SELECT payload FROM deliveries WHERE client=? AND event=? AND revision=? AND kind=?',key).fetchone()
            if row and row['payload'] != encode(event):
                raise ValueError('Conflicting event revision.')
            db.execute('INSERT OR IGNORE INTO deliveries(client,event,revision,kind,payload) VALUES(?,?,?,?,?)',(*key,encode(event)))

    @serialized
    def complete(self,event,run):
        # Result and final revision commit atomically. Concurrent retries keep the
        # first completed run and never add a second contribution for the same ID.
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            key=(event['client_id'],event['event_id'])
            row=db.execute('SELECT result FROM requests WHERE client=? AND event=?',key).fetchone()
            if row['result']:
                return json.loads(row['result'])
            retracted=db.execute("SELECT 1 FROM deliveries WHERE client=? AND event=? AND kind='retract'",key).fetchone()
            db.execute("UPDATE deliveries SET status='superseded' WHERE client=? AND event=? AND kind='event' AND status='pending'",key)
            db.execute('INSERT INTO deliveries(client,event,revision,kind,payload,status) VALUES(?,?,2,?,?,?)',
                       (*key,'event',encode(event),'superseded' if retracted else 'pending'))
            db.execute('UPDATE requests SET result=? WHERE client=? AND event=?',(encode(run),*key))
        return run

    @serialized
    def retract(self, client, event):
        with self.db() as db:
            if not db.execute('SELECT 1 FROM requests WHERE client=? AND event=?',(client,event)).fetchone():
                raise ValueError('No such event for this client.')
            db.execute("UPDATE deliveries SET status='superseded' WHERE client=? AND event=? AND kind='event' AND status='pending'",(client,event))
            db.execute('INSERT OR IGNORE INTO deliveries(client,event,revision,kind,payload) VALUES(?,?,0,?,?)',
                       (client,event,'retract',encode({'client_id':client,'event_id':event})))

    @serialized
    def reinterpret(self, client, event_id, expected_revision):
        # Append a revision of the same observation. Repeated clicks reuse that revision.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            key = (client, event_id)
            latest = db.execute("SELECT * FROM deliveries WHERE client=? AND event=? ORDER BY seq DESC LIMIT 1", key).fetchone()
            withdrawn = db.execute("SELECT 1 FROM deliveries WHERE client=? AND event=? AND kind='retract'", key).fetchone()
            if not latest or withdrawn or latest["kind"] != "event":
                raise ValueError("Only an active observation can be reinterpreted.")
            event = json.loads(latest["payload"])
            if event["entry_point"] not in {"advisor_note", "direct"} or event["outcome"] != "success":
                raise ValueError("Select a completed calculation or client note.")
            if latest["revision"] != expected_revision or latest["status"] == "pending":
                return
            event["revision"] += 1
            if event["task"] == "risk" and "result_summary" not in event:
                row = db.execute("SELECT result FROM requests WHERE client=? AND event=?", key).fetchone()
                if row and row["result"]:
                    event["result_summary"] = risk_metrics(json.loads(row["result"]).get("result", {}))
            db.execute("INSERT INTO deliveries(client,event,revision,kind,payload) VALUES(?,?,?,'event',?)",
                       (*key, event["revision"], encode(event)))

    def rows(self, client):
        with self.db() as db:
            return [dict(r) for r in db.execute('SELECT * FROM deliveries WHERE client=? ORDER BY seq',(client,))]

    def sync(self, client, *, retry_pending=False):
        # A bounded adapter operation serializes module calls. Journal appends and
        # retractions remain available while the adapter is slow or unresponsive.
        def deliver():
            try:
                adapter = adapter_for(os.environ.get('SIA_PORTFOLIO_LEARNER_FACTORY',''),str(self.directory))
                if adapter is None:
                    return 'waiting_for_module'
                deadline = monotonic() + 1.0
                # Also repair queues created by the previous integration build.
                with self.db() as db:
                    db.execute("""UPDATE deliveries SET status='pending'
                        WHERE client=? AND kind='event' AND status='applied'
                        AND json_extract(response, '$.extraction.status') IN ('pending','partial')""",
                               (client,))
                    db.execute("""UPDATE deliveries AS old SET status='superseded'
                        WHERE client=? AND kind='event' AND status='pending' AND EXISTS (
                            SELECT 1 FROM deliveries AS newer WHERE newer.client=old.client
                            AND newer.event=old.event AND (newer.kind='retract' OR
                            newer.kind='event' AND newer.revision>old.revision))""",(client,))
                rows = self.rows(client)
                rows.sort(key=lambda row:(row['kind']!='retract',row['seq']))
                process = getattr(adapter, 'retry_event', adapter.process_event) if retry_pending else adapter.process_event
                for row in rows:
                    if row['status'] != 'pending':
                        continue
                    if monotonic() > deadline:
                        return 'processing_pending'
                    # A retraction or completion may supersede a queued revision
                    # after the initial snapshot was read.
                    with self.db() as db:
                        current=db.execute('SELECT status FROM deliveries WHERE seq=?',(row['seq'],)).fetchone()
                    if current['status'] != 'pending':
                        continue
                    payload = json.loads(row['payload'])
                    reply = (adapter.retract_event(client,row['event']) if row['kind']=='retract'
                             else process(payload))
                    if not isinstance(reply,dict):
                        return 'processing_pending'
                    state=reply.get('state')
                    if (reply.get('client_id',client) != client or
                            isinstance(state,dict) and state.get('client_id',client) != client):
                        return 'module_error'
                    status = reply.get('status')
                    extraction_status = (reply.get('extraction') or {}).get('status')
                    if extraction_status in {'pending','partial'} or status in {'pending','partial'}:
                        delivery_status = 'pending'
                    elif row['kind'] == 'event' and 'needs_review' in {status, extraction_status}:
                        delivery_status = 'needs_review'
                    elif status in {'ok','success','already_processed'}:
                        delivery_status = 'applied'
                    else:
                        return 'processing_pending'
                    with self.db() as db:
                        db.execute("""UPDATE deliveries SET status=?,response=?
                            WHERE seq=? AND status='pending'""",
                                   (delivery_status, encode(reply), row['seq']))
                    if delivery_status == 'pending':
                        return 'processing_pending'
                return 'processing_pending' if any(r['status']=='pending' for r in self.rows(client)) else 'connected'
            except Exception:
                return 'module_error'
        try:
            return bounded_adapter_call(deliver)
        except TimeoutError:
            return 'module_busy'

    def view(self, client, scope=None):
        rows = self.rows(client)
        pending = sum(r['status'] == 'pending' for r in rows)
        context, status = None, 'waiting_for_module'
        try:
            def read_context():
                adapter = adapter_for(os.environ.get('SIA_PORTFOLIO_LEARNER_FACTORY',''),str(self.directory))
                if adapter is None:
                    return False,None
                return True,None if pending else checked_context(adapter.get_context(client,scope),client,scope)
            connected,context = bounded_adapter_call(read_context)
            if connected:
                status='processing_pending' if pending else 'connected'
            # Suppress a snapshot invalidated during the adapter read.
            refreshed = self.rows(client)
            if refreshed != rows:
                rows=refreshed
                pending=sum(r['status']=='pending' for r in rows)
                context=None
                status='processing_pending'
        except TimeoutError:
            context, status = None, 'module_busy'
        except Exception:
            context, status = None, 'module_error'
        # Keep the latest revision and any retraction together. Cutting the last
        # 100 raw rows could otherwise hide a prior retraction and enable it again.
        events={}
        retractions={}
        for row in rows:
            if row['kind']=='retract':
                retractions[row['event']]=row
            else:
                events[row['event']]=row
        recent=sorted(events.values(),key=lambda row:row['seq'])[-100:]
        display=sorted(recent+[retractions[r['event']] for r in recent if r['event'] in retractions],key=lambda row:row['seq'])
        review_count = sum(r['status'] == 'needs_review' and r['event'] not in retractions
                           for r in events.values())
        return {'client_id':client,'status':status,'pending_count':pending,'review_count':review_count,
                'context':context,'events':[
                    {'event_id':r['event'],'revision':r['revision'],'kind':r['kind'],
                     'delivery_status':r['status'],'event':json.loads(r['payload']),
                     'update':json.loads(r['response']) if r['response'] else None}
                    for r in display],'total_records':len(rows),'total_events':len(events)}


def get_client_learning_context(client_id: str, account_id: str | None = None) -> dict:
    """Read a known client's learned context. Never creates evidence from model output."""
    return read_analysis_context(client_id, account_id)


def analysis_scope(account_id=None, group=None):
    scope = {"account_id": account_id} if account_id else {}
    if group:
        scope.update(applicability="current", analysis_id=group)
    return scope or None


def read_analysis_context(client_id, account_id=None, group=None):
    if not valid_client(client_id):
        return {'status':'unknown_client','client_id':client_id,'context':None}
    try:
        view = Journal().view(client_id, analysis_scope(account_id, group))
        return {key:view[key] for key in ('client_id','status','pending_count','context')}
    except Exception:
        return {'status':'storage_error','client_id':client_id,'context':None}


def event_from(client, task, arguments, meta, user_text='', entry_point='direct'):
    event_id = meta.get('event_id') or uuid4().hex
    effective = {k:v for k,v in arguments.items() if k not in {'client_id','account_id'}}
    supplied = {k:v for k,v in effective.items() if k in meta.get('requested_keys',effective.keys())
                and meta.get('parameter_sources',{}).get(k) != 'default'}
    sources = {k:meta.get('parameter_sources',{}).get(k,'unknown' if k in supplied else 'default') for k in effective}
    return {'schema_version':'portfolio-learning-event-v0','event_id':event_id,
            'request_id':event_id,'revision':1,'client_id':client,'occurred_at':now(),
            'evidence_group_id':meta.get('evidence_group_id') or event_id,
            'entry_point':entry_point,'task':task,'user_text':user_text,
            'text_source':meta.get('text_source','unknown'),
            'requested_parameters':supplied,'effective_parameters':effective,
            'parameter_sources':sources,'prior_context':{},
            'scope':{'account_id':arguments['account_id']} if arguments.get('account_id') else None,
            'outcome':'received','result_reference':None}


def record_start(event):
    """Storage failure degrades separately; invalid/conflicting identity never writes."""
    intent = {k:v for k,v in event.items() if k not in {'occurred_at'}}
    try:
        journal = Journal()
        row = journal.begin(event['client_id'],event['event_id'],intent)
        event['occurred_at'] = row['created']
        if row['result']:
            cached = json.loads(row['result'])
            cached['learning'] = {'status':journal.sync(event['client_id']),'event_id':event['event_id']}
            return journal, cached
        journal.append(event)
        return journal, None
    except ValueError:
        raise
    except Exception:
        return None, None


def risk_metrics(result):
    fields = ("annual_volatility", "annual_expected_return")
    return {key: result[key] for key in fields if type(result.get(key)) in (int, float)}


def record_finish(journal, event, run):
    if journal is None:
        run['learning'] = {'status':'storage_error','event_id':event['event_id']}
        return run
    try:
        event = json.loads(encode(event))
        event.update(revision=2,outcome=run['status'],result_reference=run['run_id'])
        result = run.get('result',{})
        if result.get('client_id') == event['client_id'] and isinstance(result.get('goal'),dict):
            for key in ('target_amount','years','annual_contribution','annual_withdrawal'):
                if key in result['goal']:
                    event['effective_parameters'][key]=result['goal'][key]
                    if key not in event['requested_parameters']:
                        event['parameter_sources'][key]='default'
        if result.get('client_id') == event['client_id'] and result.get('asset_classes'):
            stock = result['asset_classes']['stock']
            event['prior_context'] = {'current_stock_weight':stock['current_weight']}
            event['effective_parameters'] = {**event['effective_parameters'],'target_stock_weight':stock['target_weight']}
            if 'target_stock_weight' not in event['requested_parameters']:
                event['parameter_sources']['target_stock_weight'] = 'default'
        if event["task"] == "risk" and run["status"] == "success":
            event["result_summary"] = risk_metrics(result)
        # Tool calls made by the model are observations of execution, not user choices.
        if event['entry_point'] == 'agent':
            event['execution_trace'] = run.get('trace',[])
        run['learning'] = {'status':'processing_pending','event_id':event['event_id']}
        run = journal.complete(event,run)
        run['learning']['status'] = journal.sync(event['client_id'])
    except Exception:
        run['learning'] = {'status':'storage_error','event_id':event['event_id']}
    return run
