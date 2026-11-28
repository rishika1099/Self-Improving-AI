"""Opt-in Gemini Developer API and explicit offline fixture adapters."""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
import re
from urllib import request, error
from self_improving_ai.agents.portfolio.model_catalog import HYDROGEN_MODEL
from .prompts import SYSTEM_PROMPT, PROMPT_VERSION
from .provider_backoff import active_wait, remember_wait

RISK_BASES = ("declared_attitude", "risk_return_tradeoff", "observed_risk_choice", "allocation_intent",
              "current_risk_attitude", "directed_inquiry", "stock_target_inquiry", "no_direction")

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["signals"],
    "properties": {
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "dimension", "value", "assertion", "scope", "evidence_id", "reason", "risk_basis", "client_topic","risk_direction","scope_quote"],
                "properties": {
                    "kind": {"type": "string"},
                    "dimension": {"type": "string", "enum": ["risk_tilt", "adjustment_style", "attention", "constraint", "correction"]},
                    "value": {"type": "string", "enum": [
                        "conservative", "balanced", "growth", "gradual", "flexible",
                        "liquidity", "income_goal", "return_priority", "loss_reaction", "capital_preservation", "leverage",
                        "risk_change", "adjustment_pace", "max_loss_limit", "budget",
                        "restricted_funds", "other",
                    ]},
                    "assertion": {"type": "string"},
                    "scope": {
                        "type": ["object", "null"],
                        "additionalProperties": False,
                        "properties": {
                            "account_id": {"type": "string"},
                            "goal_id": {"type": "string"},
                            "applicability": {"type": "string", "enum": ["ongoing", "this_analysis_only"]},
                            "unresolved": {"type": "boolean"},
                        },
                    },
                    "risk_basis": {"type": "string", "enum": list(RISK_BASES)},
                    "risk_intensity": {"type": "string", "enum": ["mild", "moderate", "strong"]},
                    "scope_quote": {"type":"string"},
                    "risk_direction": {"type":"string","enum":["lower","higher","balanced","none"]},
                    "client_topic": {"type":"string","enum":["liquidity","income_goal","return_priority","loss_reaction","capital_preservation",
                                     "leverage","risk_change","adjustment_pace","other"]},
                    "stock_target_pct": {"type": "number"},
                    "evidence_id": {"type": "integer"},
                    "reason": {"type": "string"},
                    "target_event_id": {"type": "string"},
                },
            },
        },
    },
}


def numbered_excerpts(text, *, independent_clauses=False):
    # Keep decimal numbers and identifiers intact. Spans are literal slices of the input.
    boundaries = r"(?<=[。！？!?])|(?<=\.)\s+|\n+"
    if independent_clauses:
        boundaries += r"|(?<=[;；])|(?<=[,，])\s*(?=还要|另外|同时|也请|并且|另有|and also\b|also\b|separately\b)"
    return [piece.strip() for piece in re.split(boundaries, text, flags=re.I) if piece.strip()]


class ParseError(Exception):
    def __init__(self, code, message, *, retry_at=None):
        super().__init__(message)
        self.code = code
        self.retry_at = retry_at


def _provider_parse_error(wait):
    if wait.status == 429:
        return ParseError("model_rate_limited", "Gemini quota or request limits were reached.",
                          retry_at=wait.retry_at)
    return ParseError("model_busy", "Gemini is temporarily busy.", retry_at=wait.retry_at)


@dataclass(frozen=True)
class ParsePacket:
    raw: str
    mode: str
    model: str | None = None
    prompt_version: str | None = None


class FixtureParser:
    def __init__(self, responses):
        self.responses = responses

    def __call__(self, text):
        if text not in self.responses:
            raise ParseError("fixture_missing", "No fixed response for this exact input.")
        return ParsePacket(json.dumps(self.responses[text], ensure_ascii=False), "fixture")


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GeminiParser:
    def __init__(self, model=None, *, timeout=20, api_key=None):
        self.model = model or os.environ.get("PORTFOLIO_LEARNING_GEMINI_MODEL", "")
        if self.model and not re.fullmatch(r"gemini-[a-zA-Z0-9.-]+", self.model):
            raise ValueError("Provide a Gemini Developer API model ID.")

        # Credentials are loaded for the explicit live call, never stored in output.
        self.timeout = timeout
        self._api_key = api_key

    def __call__(self, text):
        return self.parse_scoped(text, None)

    def parse_scoped(self, text, scope, *, prior_notes=()):
        key = self._api_key
        if key is None:
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        key = key.strip()
        if not key or not self.model:
            raise ParseError("model_configuration_missing", "Set a Gemini key and model ID.")
        if wait := active_wait(key, self.model):
            raise _provider_parse_error(wait)
        bindings = {field: scope[field] for field in ("account_id", "goal_id")
                    if scope and field in scope}
        instruction = SYSTEM_PROMPT
        if bindings:
            instruction += "\nCaller-verified scope IDs: " + json.dumps(bindings, ensure_ascii=False)
        parts = [{"text": text}]
        sentences = numbered_excerpts(text, independent_clauses=True)
        if len(sentences) > 1:
            parts.append({"text": json.dumps(dict(enumerate(sentences, 1)), ensure_ascii=False)})
        if prior_notes:
            parts.append({"text": json.dumps({"earlier_notes_reference_only": prior_notes}, ensure_ascii=False)})
        generation_config = {"responseMimeType":"application/json",
                             "responseJsonSchema":EXTRACTION_SCHEMA,"maxOutputTokens":2048}
        if self.model == HYDROGEN_MODEL:
            generation_config.update(thinkingConfig={"thinkingLevel":"LOW"}, maxOutputTokens=8192)
        payload = {"systemInstruction":{"parts":[{"text":instruction}]},
                   "contents":[{"role":"user","parts":parts}], "generationConfig":generation_config}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        req = request.Request(url, data=json.dumps(payload).encode(), headers={
            "x-goog-api-key":key, "Content-Type":"application/json"}, method="POST")
        try:
            with request.build_opener(_NoRedirect()).open(req, timeout=self.timeout) as resp:
                result = json.loads(resp.read(256_000))
            first = result["candidates"][0]
            if first.get("finishReason") != "STOP":
                raise ParseError("model_output_incomplete", "Model output was blocked or incomplete.")
            raw = "".join(p.get("text", "") for p in first["content"]["parts"] if not p.get("thought"))
            return ParsePacket(raw.replace(key, "[REDACTED]"), "gemini_live", self.model, PROMPT_VERSION)
        except error.HTTPError as exc:
            if exc.code in {429, 503}:
                try:
                    payload = json.loads(exc.read(65536))
                except (OSError, ValueError, TypeError):
                    payload = None
                finally:
                    exc.close()
                wait = remember_wait(key, self.model, exc.code, headers=exc.headers, payload=payload)
                raise _provider_parse_error(wait) from None
            raise ParseError("model_http_error", f"Provider HTTP status: {exc.code}.") from None
        except TimeoutError:
            raise ParseError("model_timeout", "Model call timed out.") from None
        except error.URLError as exc:
            code = "model_timeout" if isinstance(exc.reason, TimeoutError) else "model_network_error"
            raise ParseError(code, "Model endpoint did not respond successfully.") from None
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            raise ParseError("model_response_invalid", "Provider returned an invalid response.") from None
