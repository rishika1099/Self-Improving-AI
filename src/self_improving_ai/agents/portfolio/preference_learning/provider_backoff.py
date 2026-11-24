"""Share provider retry windows across Portfolio's parser and answer runner."""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import math
import re
from threading import Lock
from time import time


@dataclass(frozen=True)
class ProviderWait:
    status: int
    until: float

    @property
    def retry_at(self) -> str:
        return datetime.fromtimestamp(self.until, timezone.utc).isoformat()


_waits = {}
_lock = Lock()


def _bucket(credential, model):
    return sha256(credential.encode()).digest(), model


def active_wait(credential, model):
    key = _bucket(credential, model)
    with _lock:
        wait = _waits.get(key)
        if wait and wait.until > time():
            return wait
        _waits.pop(key, None)
    return None


def _retry_delay(headers, payload, now):
    retry_after = headers.get("Retry-After") if headers else None
    delay = None
    if retry_after:
        try:
            delay = float(retry_after)
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(retry_after)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                delay = date.timestamp() - now
            except (ValueError, TypeError, OverflowError):
                pass
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        details = error.get("details", []) if isinstance(error, dict) else []
        for item in details if isinstance(details, list) else []:
            if not isinstance(item, dict) or item.get("@type") != "type.googleapis.com/google.rpc.RetryInfo":
                continue
            retry_delay = item.get("retryDelay")
            if isinstance(retry_delay, str) and re.fullmatch(r"\d+(?:\.\d+)?s", retry_delay):
                seconds = float(retry_delay[:-1])
                if math.isfinite(seconds):
                    delay = max(seconds, delay) if delay is not None and math.isfinite(delay) else seconds
    if delay is not None and math.isfinite(delay) and 0 < delay <= 86400:
        return math.ceil(delay)
    return None


def remember_wait(credential, model, status, *, headers=None, payload=None):
    now = time()
    delay = _retry_delay(headers, payload, now) or (60 if status == 429 else 5)
    key = _bucket(credential, model)
    wait = ProviderWait(status, now + delay)
    with _lock:
        existing = _waits.get(key)
        if existing and existing.until > wait.until:
            return existing
        _waits[key] = wait
    return wait
