"""Explicit, bounded live generation check used by the connection dialog."""
from self_improving_ai.agents.portfolio.model_catalog import HYDROGEN_MODEL
from self_improving_ai.agents.portfolio.model_errors import classify_model_error, model_failure
from self_improving_ai.agents.portfolio.model_settings import portfolio_api_key
from self_improving_ai.config import settings


def test_model_connection(*, api_key: str | None = None, model: str | None = None) -> dict:
    """Probe draft values without mutating the saved key/model or process environment."""
    key = (api_key if api_key is not None else portfolio_api_key()).strip()
    selected_model = model if model is not None else settings.portfolio_model
    if not key:
        return model_failure("MISSING_MODEL_CONFIGURATION", "Enter a Gemini API key or use a saved key before testing.")
    client = None
    model_visible = False
    try:
        from google import genai
        from google.genai import types

        thinking_model = selected_model in {"gemini-3.8-flash", HYDROGEN_MODEL}
        client = genai.Client(vertexai=False, api_key=key,
                              http_options=types.HttpOptions(timeout=10000,
                                  retry_options=types.HttpRetryOptions(attempts=1)))
        config = types.GenerateContentConfig(max_output_tokens=128)
        if thinking_model:
            # Metadata availability and successful generation are separate checks.
            try:
                info = client.models.get(model=selected_model, config=types.GetModelConfig(
                    http_options=types.HttpOptions(timeout=1500)))
                model_visible = bool(info.name)
            except Exception as exc:
                if getattr(exc, "code", None) in {400, 401, 403, 404}:
                    return {**classify_model_error(exc), "model": selected_model}
            config = types.GenerateContentConfig(max_output_tokens=1024 if selected_model == HYDROGEN_MODEL else 512,
                thinking_config=types.ThinkingConfig(thinking_level="LOW"))
        response = client.models.generate_content(model=selected_model, contents="Reply with OK only.",
                                                  config=config)
        if not any(part.text and part.text.strip() and not part.thought
                   for candidate in (getattr(response, "candidates", None) or []) if candidate.content
                   for part in (candidate.content.parts or [])):
            return {**model_failure("EMPTY_MODEL_RESPONSE",
                "Gemini returned no usable text. Try again or choose another model.", retryable=True),
                "model": selected_model}
        return {"status": "success", "source": "gemini", "model": selected_model,
                "message": "Gemini accepted a generation request. Connection verified."}
    except Exception as exc:
        failure = classify_model_error(exc)
        error = failure["error"]
        if model_visible and error["code"] in {"MODEL_BUSY", "MODEL_PROVIDER_UNAVAILABLE", "MODEL_TIMEOUT"}:
            detail = error.get("provider_http_status")
            suffix = f" (HTTP {detail})" if detail else ""
            outcome = "timed out" if error["code"] == "MODEL_TIMEOUT" else "failed"
            error["message"] = (f"Google recognizes {selected_model} for this key. "
                                f"The generation request {outcome}{suffix}. Retry later or test another model.")
            failure["model_visible"] = True
        return {**failure, "model": selected_model}
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass  # Cleanup must not replace the bounded, sanitized probe result.
