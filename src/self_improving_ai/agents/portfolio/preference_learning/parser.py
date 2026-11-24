"""Opt-in Gemini Developer API and explicit offline fixture adapters."""
from __future__ import annotations
from dataclasses import dataclass
import json
import os
import re
from urllib import request, error
from .prompts import SYSTEM_PROMPT, PROMPT_VERSION


class ParseError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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

    def parse_scoped(self, text, scope):
        key = self._api_key
        if key is None:
            key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""
        key = key.strip()
        if not key or not self.model:
            raise ParseError("model_configuration_missing", "Set a Gemini key and model ID.")
        bindings = {field: scope[field] for field in ("account_id", "goal_id")
                    if scope and field in scope}
        instruction = SYSTEM_PROMPT
        if bindings:
            instruction += "\nCaller-verified scope IDs: " + json.dumps(bindings, ensure_ascii=False)
        payload = {"systemInstruction":{"parts":[{"text":instruction}]},
                   "contents":[{"role":"user","parts":[{"text":text}]}],
                   "generationConfig":{"responseMimeType":"application/json","maxOutputTokens":2048}}
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
            raise ParseError("model_http_error", f"Provider HTTP status: {exc.code}.") from None
        except TimeoutError:
            raise ParseError("model_timeout", "Model call timed out.") from None
        except error.URLError as exc:
            code = "model_timeout" if isinstance(exc.reason, TimeoutError) else "model_network_error"
            raise ParseError(code, "Model endpoint did not respond successfully.") from None
        except (KeyError, IndexError, ValueError, TypeError, AttributeError):
            raise ParseError("model_response_invalid", "Provider returned an invalid response.") from None
