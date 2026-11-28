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
from self_improving_ai.agents.portfolio.analysis_focus import previous_reference, working_index
from self_improving_ai.agents.portfolio.risk_scoring import SCORING_POLICY, risk_index
from self_improving_ai.agents.portfolio.score_history import replay_score_history
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


def operation_numbers(rows):
    """Number the first saved revision of each operation using the durable journal sequence."""
    numbers = {}
    for row in rows:
        if row["kind"] == "event" and row["event"] not in numbers:
            numbers[row["event"]] = f"UNI-{row['seq']:06d}"
    return numbers


def cited_operations(context, conflicts):
    """Collect source links from the profile, the scoring ledger and the conflict cards."""
    sources = {item["event_id"] for group in conflicts.values() for item in group.values()}
    for key in ("team_declared", "tentative_preferences", "recent_attention", "client_cues", "explicit_constraints"):
        for item in context.get(key) or []:
            if isinstance(item, dict):
                sources.add(item.get("source_event_id"))
                sources.update(source.get("event_id") for source in item.get("sources") or []
                               if isinstance(source, dict))
    score_groups = [context.get("risk_preference_scores"), *(context.get("risk_source_scores") or {}).values()]
    for group in score_groups:
        for card in group or []:
            sources.update(row.get("event_id") for row in card.get("evidence") or [])
    return sources


def conflict_sources(context, events):
    """Match a profile conflict to the original evidence, including discarded signals."""
    conflicts = context.get("conflicts", [])
    signal_ids = {ref for conflict in conflicts if conflict.get("kind") != "applicable_scopes_disagree"
                  for ref in (conflict.get("evidence_ids") or [])}
    event_ids = {ref for conflict in conflicts if conflict.get("kind") == "applicable_scopes_disagree"
                 for ref in (conflict.get("evidence_ids") or [])}

    def source(row):
        event = json.loads(row["payload"])
        return {"event_id": row["event"], "entry_point": event.get("entry_point"),
                "task": event.get("task"), "occurred_at": event.get("occurred_at"),
                "user_text": event.get("user_text"), "scope": event.get("scope")}

    by_event = {ref: source(events[ref]) for ref in event_ids if ref in events}
    by_signal = {}
    for row in reversed(events.values()):
        if not signal_ids:
            break
        if not row["response"]:
            continue
        extraction = json.loads(row["response"]).get("extraction") or {}
        for signal in extraction.get("signals") or []:
            ref = signal.get("signal_id")
            if ref in signal_ids:
                by_signal[ref] = {**source(row), "kind": signal.get("kind"), "value": signal.get("value"),
                                  "evidence_text": signal.get("evidence_text"), "basis": signal.get("basis"),
                                  "scope": signal.get("scope")}
                signal_ids.remove(ref)
    return {"events": by_event, "signals": by_signal}


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
                CREATE TABLE IF NOT EXISTS risk_calibrations(
                    client TEXT, revision INTEGER, chosen INTEGER, baseline REAL, created TEXT, mode TEXT,
                    policy INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(client,revision));
                CREATE TABLE IF NOT EXISTS risk_displays(
                    client TEXT PRIMARY KEY, version INTEGER, revision INTEGER, score REAL, confirmed TEXT);
                CREATE TABLE IF NOT EXISTS learning_resets(
                    client TEXT PRIMARY KEY, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT, event TEXT,
                    revision INTEGER, kind TEXT, payload TEXT, status TEXT DEFAULT 'pending',
                    response TEXT, processed_at TEXT, UNIQUE(client,event,revision,kind));
            ''')
            migrations = {"mode":"TEXT", "policy":"INTEGER NOT NULL DEFAULT 1"}
            if not migrations.keys() <= {r['name'] for r in db.execute('PRAGMA table_info(risk_calibrations)')}:
                db.execute('BEGIN IMMEDIATE')
                columns = {r['name'] for r in db.execute('PRAGMA table_info(risk_calibrations)')}
                for name, sql in migrations.items():
                    if name not in columns:
                        db.execute(f'ALTER TABLE risk_calibrations ADD COLUMN {name} {sql}')
            if 'processed_at' not in {r['name'] for r in db.execute('PRAGMA table_info(deliveries)')}:
                if not db.in_transaction:
                    db.execute('BEGIN IMMEDIATE')
                if 'processed_at' not in {r['name'] for r in db.execute('PRAGMA table_info(deliveries)')}:
                    db.execute('ALTER TABLE deliveries ADD COLUMN processed_at TEXT')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @serialized
    def begin(self, client, event_id, intent):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO requests VALUES(?,?,?,?,NULL)',
                       (client,event_id,encode(intent),now()))
            row = db.execute('SELECT * FROM requests WHERE client=? AND event=?',
                             (client,event_id)).fetchone()
            if row['intent'] != encode(intent):
                raise ValueError('This event ID already belongs to different input.')
            if row['result'] is None:
                event = {**intent, 'occurred_at': row['created']}
                self._append(db, event)
            return dict(row)

    @staticmethod
    def generation(db, client):
        row = db.execute("SELECT generation FROM learning_resets WHERE client=?", (client,)).fetchone()
        return row["generation"] if row else 0

    @serialized
    def clear(self, client, generation):
        if not _adapter_gate.acquire(blocking=False):
            raise ValueError("The learning module is processing records. Try clearing them again shortly.")
        try:
            adapter = adapter_for(os.environ.get('SIA_PORTFOLIO_LEARNER_FACTORY', ''), str(self.directory))
            attach = getattr(adapter, "attach_reset_storage", None)
            if not attach and (adapter is not None or (self.directory / 'learner' / 'preferences.sqlite3').exists()):
                raise ValueError("The current learning connection cannot clear the full history. Reconnect a module that supports clearing and try again.")
            with self.db() as db:
                if attach:
                    attach(db)
                db.execute("BEGIN IMMEDIATE")
                if generation != self.generation(db, client):
                    raise ValueError("These records were already cleared. Refresh to see the current records.")
                if attach:
                    adapter.delete_attached_client(db, client)
                for table in ("requests", "deliveries", "risk_calibrations", "risk_displays"):
                    db.execute(f"DELETE FROM {table} WHERE client=?", (client,))
                db.execute("""INSERT INTO learning_resets VALUES (?,?)
                    ON CONFLICT(client) DO UPDATE SET generation=excluded.generation""", (client, generation + 1))
            return {"status": "success", "generation": generation + 1}
        finally:
            _adapter_gate.release()

    def calibration(self, db, client):
        row = db.execute("SELECT revision,chosen,baseline,created,mode,policy FROM risk_calibrations WHERE client=? "
                         "ORDER BY revision DESC LIMIT 1", (client,)).fetchone()
        return dict(row) if row else {"revision": 0, "chosen": None, "baseline": None, "mode": "adaptive",
                                     "policy": SCORING_POLICY}

    def calibrate(self, client, chosen, expected_revision, profile_version, mode="adaptive", generation=0):
        with self.db() as db:
            sequence = db.execute("SELECT max(seq) FROM deliveries WHERE client=?", (client,)).fetchone()[0]
        view = self.view(client)
        context = view.get("context") or {}
        index = context.get("working_risk_index")
        if not index or context.get("profile_version") != profile_version:
            raise ValueError("The client profile changed or is updating. Refresh before adjusting the score.")
        baseline = index["reference"] if index["reference"] is not None else 50
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if generation != self.generation(db, client):
                raise ValueError("The learning records were cleared. Refresh before setting the score.")
            if sequence != db.execute("SELECT max(seq) FROM deliveries WHERE client=?", (client,)).fetchone()[0]:
                raise ValueError("A new operation just arrived. Check the latest score and try again.")
            if self.calibration(db, client)["revision"] != expected_revision:
                raise ValueError("The score was already adjusted. Refresh and try again.")
            db.execute("INSERT INTO risk_calibrations(client,revision,chosen,baseline,created,mode,policy) VALUES(?,?,?,?,?,?,?)",
                       (client, expected_revision + 1, chosen, baseline, now(),
                        mode if chosen is not None else "adaptive", index.get("policy", 1)))
        return {"status": "success", "revision": expected_revision + 1}

    @serialized
    def append(self, event):
        with self.db() as db:
            self._append(db, event)

    @staticmethod
    def _append(db, event):
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
            row=db.execute('SELECT created,result FROM requests WHERE client=? AND event=?',key).fetchone()
            if row is None or row['created'] != event['occurred_at']:
                return {**run, 'learning': {'status': 'cleared', 'event_id': event['event_id']}}
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
            if event["entry_point"] not in {"advisor_note", "direct", "agent"} or event["outcome"] != "success":
                raise ValueError("Select a completed calculation or client note.")
            if latest["revision"] != expected_revision or latest["status"] == "pending":
                return
            event["revision"] += 1
            if event["task"] == "risk" and "current_stock_weight" not in event["prior_context"]:
                row = db.execute("SELECT result FROM requests WHERE client=? AND event=?", key).fetchone()
                if row and row["result"]:
                    add_risk_execution(event, json.loads(row["result"]).get("result", {}))
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
                    elif row['kind'] == 'event' and ('needs_review' in {status, extraction_status}
                            or any(i.get('code') == 'incomplete_coverage' for i in (reply.get('extraction') or {}).get('issues', []))):
                        delivery_status = 'needs_review'
                    elif status in {'ok','success','already_processed'}:
                        delivery_status = 'applied'
                    else:
                        return 'processing_pending'
                    with self.db() as db:
                        db.execute("""UPDATE deliveries SET status=?,response=?,processed_at=?
                            WHERE seq=? AND status='pending'""",
                                   (delivery_status, encode(reply), now() if delivery_status != 'pending' else None,
                                    row['seq']))
                    if delivery_status == 'pending':
                        return 'processing_pending'
                return 'processing_pending' if any(r['status']=='pending' for r in self.rows(client)) else 'connected'
            except Exception:
                return 'module_error'
        try:
            return bounded_adapter_call(deliver)
        except TimeoutError:
            return 'module_busy'

    @serialized
    def view(self, client, scope=None, *, page=None, event_id=None, operation_number=None):
        with self.db() as db:
            generation = self.generation(db, client)
        rows = self.rows(client)
        pending = sum(r['status'] == 'pending' for r in rows)
        context, status = None, 'waiting_for_module'
        try:
            def read_context():
                adapter = adapter_for(os.environ.get('SIA_PORTFOLIO_LEARNER_FACTORY',''),str(self.directory))
                if adapter is None:
                    return False,None
                read = getattr(adapter, 'get_analysis_context', adapter.get_context)
                return True,None if pending else checked_context(read(client,scope),client,scope)
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
        if context is not None:
            observations = context.pop("risk_observations", None)
            sources = context.get("risk_source_scores", {})
            cards = sources.get("operations", context.get("risk_preference_scores", []))
            notes = {encode(c.get("scope")):c for c in sources.get("notes", [])}
            combined = {encode(c.get("scope")):c for c in context.get("risk_preference_scores", [])}
            general = next((c for c in cards if not c.get("scope")), None)
            if general is not None:
                household = [r for r in observations or [] if not r.get("scope")]
                note = notes.get("null", {})
                old_index = working_index(general, {}, note, combined.get("null"))
                with self.db() as db:
                    adjustment = self.calibration(db, client)
                    policy = SCORING_POLICY if observations is not None else 1
                    if adjustment["chosen"] is not None and (
                            not adjustment["mode"] or adjustment["policy"] < policy):
                        old = old_index["reference"] if adjustment["mode"] else previous_reference(general, note)
                        if adjustment["policy"] == 2 and observations is not None:
                            old = risk_index(household, {}, policy=2)["reference"]
                        movement = old - adjustment["baseline"] if old is not None else 0
                        reference = risk_index(household, {})["reference"] if observations is not None else old_index["reference"]
                        adjustment.update(baseline=reference - movement, mode=adjustment["mode"] or "adaptive", policy=policy)
                        db.execute("UPDATE risk_calibrations SET baseline=?,mode=?,policy=? WHERE client=? AND revision=?",
                                   (adjustment["baseline"], adjustment["mode"], policy, client, adjustment["revision"]))
                if observations is not None:
                    context["working_risk_index"] = self.scored_index(household, adjustment)
                    scoped_rows = {}
                    for row in observations:
                        applicable = row.get("scope")
                        if applicable and all((scope or {}).get(key) == value for key, value in applicable.items()):
                            scoped_rows.setdefault(encode(applicable), []).append(row)
                    for key in sorted(scoped_rows, key=lambda key: len(json.loads(key)), reverse=True):
                        scoped = self.scored_index(scoped_rows[key], {"chosen": None})
                        if scoped["status"] != "insufficient":
                            context["scoped_risk_index"] = {**scoped, "scope": json.loads(key)}
                            break
                else:
                    context["working_risk_index"] = working_index(general, adjustment, notes.get("null"), combined.get("null"))
                    # Compatibility for adapters that supply only the earlier score cards.
                    by_scope = {encode(c["scope"]):c for c in cards}
                    for key, scoped_note in notes.items():
                        by_scope.setdefault(key, {"scope":scoped_note["scope"], "status":"insufficient"})
                    specific = [c for k,c in by_scope.items() if c.get("scope") and
                                (c.get("score") is not None or c.get("status") == "conflict"
                                 or notes.get(k, {}).get("score") is not None)]
                    for card in sorted(specific, key=lambda c: len(c["scope"]), reverse=True):
                        key = encode(card["scope"])
                        scoped = working_index(card, {"chosen": None}, notes.get(key), combined.get(key))
                        if scoped["status"] != "insufficient":
                            context["scoped_risk_index"] = {**scoped, "scope": card["scope"]}
                            break
        # Keep each operation and its withdrawal together even across page boundaries.
        events={}
        retractions={}
        for row in rows:
            if row['kind']=='retract':
                retractions[row['event']]=row
            else:
                events[row['event']]=row
        ordered=sorted(events.values(),key=lambda row:row['seq'])
        numbers = operation_numbers(rows)
        extra = {}
        if page is None:
            recent = ordered[-100:]
        else:
            page_size = 10
            pages = max(1, (len(ordered) + page_size - 1) // page_size)
            page = min(page, pages)
            if operation_number is not None:
                event_id = next((event for event, number in numbers.items() if number == operation_number), None)
            if event_id is not None or operation_number is not None:
                located = event_id in events
                if located:
                    position = next(i for i, row in enumerate(reversed(ordered)) if row['event'] == event_id)
                    page = position // page_size + 1
                    extra["located_event_id"] = event_id
                extra["located_event"] = located
            offset = (page - 1) * page_size
            recent = list(reversed(ordered))[offset:offset + page_size]
            latest = next((r for r in reversed(ordered) if r["event"] not in retractions), None)
            extra.update({"pagination": {"page":page,"pages":pages,"page_size":page_size},
                     "latest_event": {"event":json.loads(latest["payload"]),
                                      "delivery_status":latest["status"]} if latest else None,
                     "pending_records": [{"delivery_status":"pending",
                                          "update":json.loads(r["response"]) if r["response"] else None}
                                         for r in rows if r["status"] == "pending"]})
            if context and context.get("conflicts"):
                extra["conflict_sources"] = conflict_sources(context, events)
            if context:
                extra["source_operation_numbers"] = {
                    event: numbers[event] for event in cited_operations(context, extra.get("conflict_sources", {}))
                    if event in numbers}
        display=sorted(recent+[retractions[r['event']] for r in recent if r['event'] in retractions],key=lambda row:row['seq'])
        review_count = sum(r['status'] == 'needs_review' and r['event'] not in retractions
                           for r in events.values())
        return {'client_id':client,'status':status,'generation':generation,'pending_count':pending,'review_count':review_count,
                'context':context,'events':[
                    {'event_id':r['event'],'operation_number':numbers.get(r['event']),
                     'revision':r['revision'],'kind':r['kind'],
                     'delivery_status':r['status'],'event':json.loads(r['payload']),
                     'update':json.loads(r['response']) if r['response'] else None}
                    for r in display],'total_records':len(rows),'total_events':len(events), **extra}


    @serialized
    def display_view(self, client, scope=None, *, page=1, event_id=None, operation_number=None):
        """Keep only the last verified household number available to the dashboard during outages."""
        view = self.view(client, scope, page=page, event_id=event_id, operation_number=operation_number)
        context = view.get("context") or {}
        index = context.get("working_risk_index")
        visible_events = {r["event_id"] for r in view["events"]}
        deliveries, calibrations = [], []
        with self.db() as db:
            if view["status"] == "connected" and index is not None:
                db.execute("""INSERT INTO risk_displays VALUES(?,?,?,?,?)
                    ON CONFLICT(client) DO UPDATE SET version=excluded.version,revision=excluded.revision,
                    score=excluded.score,confirmed=excluded.confirmed
                    WHERE (excluded.version,excluded.revision) >= (version,revision)""",
                    (client, context["profile_version"], index["calibration"]["revision"], index["effective"], now()))
            else:
                row = db.execute("SELECT score,confirmed FROM risk_displays WHERE client=?", (client,)).fetchone()
                if row:
                    view["last_confirmed_display"] = dict(row)
            if visible_events:
                deliveries = db.execute("""SELECT seq,event,revision,kind,processed_at,
                    coalesce(json_extract(payload,'$.occurred_at'), json_extract(response,'$.state.updated_at')) AS occurred_at,
                    json_extract(response,'$.extraction') AS extraction
                    FROM deliveries WHERE client=? AND status IN ('applied','needs_review')
                    AND (kind='event' OR json_type(response,'$.state.rule_version')='text') ORDER BY seq""",
                    (client,)).fetchall()
                calibrations = db.execute("""SELECT created,chosen,mode FROM risk_calibrations
                    WHERE client=? ORDER BY revision""", (client,)).fetchall()
        impacts = replay_score_history(client, deliveries, calibrations, visible_events)
        for row in view["events"]:
            scores = impacts.get(row["event_id"], [])
            row["score_effects"] = [effect for effect in scores if effect["kind"] == row["kind"]
                                    and (row["delivery_status"] != "pending" or effect["revision"] < row["revision"])]
        return view

    @serialized
    def export(self, client):
        view = self.view(client)
        deliveries = self.rows(client)
        return {"schema_version": "portfolio-integration-export-v1", "view": view,
                "deliveries": deliveries, "operation_numbers": operation_numbers(deliveries)}

    @staticmethod
    def scored_index(observations, calibration):
        index = risk_index(observations, calibration)
        daily = risk_index([row for row in observations if row["entry_point"] != "advisor_note"], {})
        index.update(automatic=daily["reference"], policy=SCORING_POLICY)
        return index


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
        return journal, None
    except ValueError:
        raise
    except Exception:
        return None, None


def risk_metrics(result):
    fields = ("annual_volatility", "annual_expected_return")
    return {key: result[key] for key in fields if type(result.get(key)) in (int, float)}


def add_risk_execution(event, result):
    event["result_summary"] = risk_metrics(result)
    comparison = result.get("scenario_comparison", {})
    if result.get("client_id") == event["client_id"] and comparison.get("status") == "success":
        event["prior_context"]["current_stock_weight"] = result["weights"]["stock"]
        event["effective_parameters"]["target_stock_weight"] = comparison["weights"]["stock"]
        if "target_stock_weight" not in event["requested_parameters"]:
            event["parameter_sources"]["target_stock_weight"] = "default"


def record_finish(journal, event, run):
    if journal is None:
        run['learning'] = {'status':'storage_error','event_id':event['event_id']}
        return run
    try:
        event = json.loads(encode(event))
        event.update(revision=2,outcome=run['status'],result_reference=run['run_id'],
                     operation_scoring="equity-distance-v1")
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
            add_risk_execution(event, result)
        # Tool calls made by the model are observations of execution, not user choices.
        if event['entry_point'] == 'agent':
            event['execution_trace'] = run.get('trace',[])
        run['learning'] = {'status':'processing_pending','event_id':event['event_id']}
        run = journal.complete(event,run)
        if run['learning']['status'] != 'cleared':
            run['learning']['status'] = journal.sync(event['client_id'])
    except Exception:
        run['learning'] = {'status':'storage_error','event_id':event['event_id']}
    return run
