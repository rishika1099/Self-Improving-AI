"""SQLite runtime. Each successful write commits input, interpretation and state together."""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .context import build_context
from .learner import load_rules, rebuild_state, result_from, update_state
from .parser import ParsePacket
from .schemas import canonical, checked_id, event_scope, issue, validate_event
from .signals import extract_signals


class PortfolioLearner:
    def __init__(self, work_dir, rules_path=None, parser=None):
        self.rules = load_rules(rules_path)
        self.parser = parser
        directory = Path(work_dir)
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "preferences.sqlite3"
        signature = hashlib.sha256(canonical(self.rules).encode()).hexdigest()
        with self._write() as db:
            for sql in (
                "CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                """CREATE TABLE IF NOT EXISTS events(
                    client TEXT, event TEXT, rev INTEGER, raw_digest TEXT NOT NULL, extraction TEXT NOT NULL,
                    PRIMARY KEY(client,event,rev))""",
                """CREATE TABLE IF NOT EXISTS retractions(
                    client TEXT, event TEXT, removed_at TEXT NOT NULL, PRIMARY KEY(client,event))""",
                "CREATE TABLE IF NOT EXISTS profiles(client TEXT PRIMARY KEY, state TEXT NOT NULL)",
            ):
                db.execute(sql)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('rules_sha256',?)", (signature,))
            actual = db.execute("SELECT value FROM metadata WHERE key='rules_sha256'").fetchone()[0]
            if actual != signature and self.rules["rule_version"] == "portfolio-preference-v0.2":
                # Upgrade only when removing the new hint rules reproduces the exact old rules.
                legacy = deepcopy(self.rules)
                legacy["rule_version"] = "portfolio-preference-v0.1"
                legacy["rules"] = {k: v for k, v in legacy["rules"].items() if not k.startswith("hint_")}
                if actual == hashlib.sha256(canonical(legacy).encode()).hexdigest():
                    db.execute("UPDATE metadata SET value=? WHERE key='rules_sha256'", (signature,))
                    actual = signature
            if actual != signature:
                raise ValueError("This database uses different rules. Replay exports explicitly into a new workspace.")

    def _connect(self):
        return sqlite3.connect(self.db, timeout=5)

    @contextmanager
    def _write(self):
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    def _state(self, db, client):
        row = db.execute("SELECT state FROM profiles WHERE client=?", (client,)).fetchone()
        return json.loads(row[0]) if row else rebuild_state({"client_id":client}, [], self.rules)["state"]

    def _history(self, db, client):
        return {
            "extractions":[json.loads(row[0]) for row in db.execute(
                "SELECT extraction FROM events WHERE client=? ORDER BY event,rev", (client,))],
            "retracted_event_ids":[row[0] for row in db.execute(
                "SELECT event FROM retractions WHERE client=? ORDER BY event", (client,))],
        }

    def _save(self, db, client, state):
        db.execute("INSERT INTO profiles VALUES (?,?) ON CONFLICT(client) DO UPDATE SET state=excluded.state",
                   (client, canonical(state)))

    def _plain(self, state, status, code, message):
        return {"status":status, "base_profile_version":state["profile_version"] if state else None,
                "state":state, "applied":[], "ignored":[], "issues":[issue(code,message)], "changes":[]}

    def _preflight(self, db, event, digest, version, *, retry_pending=False):
        client, eid, rev = (event[k] for k in ("client_id", "event_id", "revision"))
        state = self._state(db, client)
        withdrawn = db.execute("SELECT 1 FROM retractions WHERE client=? AND event=?",
                               (client, eid)).fetchone()
        row = db.execute("SELECT raw_digest,extraction FROM events WHERE client=? AND event=? AND rev=?",
                         (client,eid,rev)).fetchone()
        if row:
            if row[0] != digest:
                return self._plain(state, "conflict", "revision_conflict", "Same event and revision have different content.")
            extraction = json.loads(row[1])
            if retry_pending and extraction["status"] in {"pending", "partial"} and not withdrawn:
                newer = db.execute("SELECT 1 FROM events WHERE client=? AND event=? AND rev>?",
                                   (client, eid, rev)).fetchone()
                if not newer:
                    if version is not None and version != state["profile_version"]:
                        return self._plain(state, "conflict", "stale_profile", "Reload client context before retrying.")
                    return None
            res = self._plain(state,"already_processed","duplicate","Same input already processed.")
            res["extraction"] = extraction
            if not withdrawn and any(s["kind"] == "correction" for s in extraction["signals"]):
                replay = rebuild_state(state, self._history(db, client), self.rules)
                review = [i for i in replay["issues"] if i.get("event_id") == eid]
                if review:
                    res["status"] = "needs_review"
                    res["issues"] = review
            return res
        if version is not None and version != state["profile_version"]:
            return self._plain(state,"conflict","stale_profile","Reload client context before submitting a new update.")
        if withdrawn:
            return self._plain(state,"conflict","event_retracted","A withdrawn event cannot be revised. Use a new event.")
        row = db.execute("SELECT rev,extraction FROM events WHERE client=? AND event=? ORDER BY rev DESC LIMIT 1",
                         (client,eid)).fetchone()
        if row:
            if rev <= row[0]:
                return self._plain(state,"conflict","stale_revision","Use a revision higher than the last recorded one.")
            previous = json.loads(row[1])["event"]
            for field in ("request_id","evidence_group_id","occurred_at"):
                if previous[field] != event[field]:
                    return self._plain(state,"conflict","revision_identity_changed",
                                       f"{field} must stay constant across revisions.")
        return None

    def _reusable_initial_parse(self, db, event):
        if event["outcome"] not in {"success", "error"} or event["revision"] == 1:
            return None
        row = db.execute(
            """SELECT extraction FROM events
               WHERE client=? AND event=? AND rev<? ORDER BY rev DESC LIMIT 1""",
            (event["client_id"], event["event_id"], event["revision"]),
        ).fetchone()
        if row is None:
            return None
        previous = json.loads(row[0])
        earlier = previous["event"]
        parser = previous["parser"]
        if (earlier["outcome"] != "received"
                or previous["status"] != "ok"
                or parser.get("validation") != "ok"
                or not isinstance(parser.get("raw"), str)):
            return None
        fields = ("schema_version", "task", "entry_point", "text_source", "user_text")
        if any(earlier.get(field) != event.get(field) for field in fields):
            return None
        if event_scope(earlier) != event_scope(event):
            return None
        packet = ParsePacket(parser["raw"], parser["mode"], parser.get("model"),
                             parser.get("prompt_version"))
        return earlier["revision"], packet

    def process_event(self, raw_event, *, expected_profile_version=None, retry_pending=False):
        """An optional version supports optimistic concurrency across callers."""
        try:
            event, _ = validate_event(raw_event)
            digest = hashlib.sha256(canonical(raw_event).encode()).hexdigest()
        except (ValueError, TypeError, OverflowError) as exc:
            return self._plain(None,"invalid","invalid_event",str(exc))
        client = event["client_id"]
        try:
            with closing(self._connect()) as db:
                db.execute("BEGIN")
                conflict = self._preflight(db,event,digest,expected_profile_version,retry_pending=retry_pending)
                if conflict:
                    return conflict
                base_version = self._state(db,client)["profile_version"]
                reusable = self._reusable_initial_parse(db, event)
            if reusable is None:
                extraction = extract_signals(raw_event, self.parser)
            else:
                source_revision, packet = reusable
                # Revalidate the stored text semantics and derive signals from the
                # current execution fields. The current revision owns the signals.
                extraction = extract_signals(raw_event, lambda _: packet)
                if extraction["parser"].get("validation") == "ok":
                    extraction["parser"]["reused_from_revision"] = source_revision
                else:
                    extraction = extract_signals(raw_event, self.parser)
            with self._write() as db:
                conflict = self._preflight(db,event,digest,base_version,retry_pending=retry_pending)
                if conflict:
                    return conflict
                state = self._state(db,client)
                history = self._history(db, client)
                key = (client, event["event_id"], event["revision"])
                saved = next((x for x in history["extractions"]
                              if (x["event"]["client_id"], x["event"]["event_id"], x["event"]["revision"]) == key),
                             None)
                if saved is not None:
                    if extraction["status"] in {"pending", "partial"}:
                        response = self._plain(state, extraction["status"], "retry_unresolved",
                                               "The input still needs a usable interpretation.")
                        response["issues"] += extraction["issues"]
                        response["extraction"] = extraction
                        return response
                    history["extractions"].remove(saved)
                result = update_state(state, extraction, self.rules, history)
                db.execute("""INSERT INTO events VALUES (?,?,?,?,?)
                    ON CONFLICT(client,event,rev) DO UPDATE SET extraction=excluded.extraction""",
                           (*key, digest, canonical(extraction)))
                self._save(db,client,result["state"])
                result["extraction"] = extraction
                return result
        except sqlite3.Error:
            return self._plain(None,"storage_error","event_not_saved",
                               "The write could not be completed; any last committed profile is preserved.")

    def get_context(self, client_id, scope=None):
        checked_id(client_id, "client_id")
        with closing(self._connect()) as db:
            return build_context(self._state(db,client_id),scope)

    def retract_event(self, client_id, event_id, *, expected_profile_version=None, allow_missing=False):
        try:
            checked_id(client_id, "client_id")
            checked_id(event_id, "event_id")
        except ValueError as exc:
            return self._plain(None,"invalid","invalid_retraction",str(exc))
        try:
            with self._write() as db:
                state = self._state(db,client_id)
                history = self._history(db,client_id)
                if event_id in history["retracted_event_ids"]:
                    return self._plain(state,"already_processed","duplicate_retraction","Event already withdrawn.")
                if expected_profile_version is not None and expected_profile_version != state["profile_version"]:
                    return self._plain(state,"conflict","stale_profile","Reload client context before retracting.")
                if not allow_missing and not any(x["event"]["event_id"] == event_id for x in history["extractions"]):
                    return self._plain(state,"needs_review","unknown_event","No matching event for this client.")
                now = datetime.now(timezone.utc).isoformat()
                db.execute("INSERT INTO retractions VALUES (?,?,?)", (client_id,event_id,now))
                history["retracted_event_ids"].append(event_id)
                after = rebuild_state({**state,"profile_version":state["profile_version"]+1},history,self.rules)
                after["state"]["updated_at"] = now
                self._save(db,client_id,after["state"])
                return result_from(state,after)
        except sqlite3.Error:
            return self._plain(None,"storage_error","retraction_not_saved",
                               "The write could not be completed; any last committed profile is preserved.")

    def export_client(self, client_id):
        checked_id(client_id, "client_id")
        with closing(self._connect()) as db:
            db.execute("BEGIN")
            history = self._history(db,client_id)
            return {"client_id":client_id,"rules":deepcopy(self.rules),"state":self._state(db,client_id),
                    "evidence_history":history,"retraction_log":[{"event_id":row[0],"removed_at":row[1]}
                    for row in db.execute("SELECT event,removed_at FROM retractions WHERE client=? ORDER BY event",
                                          (client_id,))]}
