"""Local LLM provider (Phase 8, D-030): one call, ``generate(system, user)``, to the local Ollama server.

- **Local and private:** the server URL must name a local host (``Settings.ollama_base_url``). Requests go straight
  to it: proxies from the environment are ignored and redirects are refused, so a prompt (the question and policy
  evidence) can never be sent anywhere else.
- **Deterministic:** temperature 0 and a fixed seed, set here and not configurable. The context window is fixed
  (``CONTEXT_TOKENS``) so Ollama never truncates the start of the prompt, where the instructions are; callers keep
  their prompts within ``MAX_PROMPT_CHARS``.
- **Errors** are ``LLMError`` with a reason. They never contain the prompt, the answer or the server's message.
- **Standard library only** (``urllib``): one JSON POST needs no new dependency.
"""

import json
import logging
import socket
import urllib.error
import urllib.request
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.config import Settings

logger = logging.getLogger(__name__)

TEMPERATURE = 0.0
SEED = 0
CONTEXT_TOKENS = 8192
# A conservative bound on system + user prompt characters, so the prompt and the answer fit CONTEXT_TOKENS.
MAX_PROMPT_CHARS = 20_000


class LLMFailure(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"  # the server cannot be reached
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"  # the server does not have the configured model
    TIMEOUT = "TIMEOUT"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"  # any other HTTP error from the server
    INVALID_RESPONSE = "INVALID_RESPONSE"  # not the expected JSON
    EMPTY_RESPONSE = "EMPTY_RESPONSE"  # no answer text
    PROMPT_TOO_LARGE = "PROMPT_TOO_LARGE"


class LLMError(Exception):
    def __init__(self, reason: LLMFailure) -> None:
        self.reason = reason
        super().__init__(f"LLM call failed: {reason}")


class LLMOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    output_tokens: int | None = None


class LLMProvider(Protocol):
    """The provider boundary: an explicit model id and one generation call."""

    model_id: str

    def generate(self, system: str, user: str, json_schema: dict[str, Any] | None = None) -> LLMOutput: ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # urllib then raises HTTPError: a prompt is never re-sent elsewhere


class OllamaProvider:
    """The configured model on the local Ollama server (``POST /api/chat``, not streamed)."""

    def __init__(self, settings: Settings) -> None:
        self.model_id = settings.llm_model
        self._url = f"{settings.ollama_base_url}/api/chat"
        self._timeout = settings.llm_timeout_seconds
        self._max_output_tokens = settings.llm_max_output_tokens
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def generate(self, system: str, user: str, json_schema: dict[str, Any] | None = None) -> LLMOutput:
        """One answer. With ``json_schema``, Ollama constrains the output to JSON of that schema (Phase 9)."""
        if len(system) + len(user) > MAX_PROMPT_CHARS:
            raise LLMError(LLMFailure.PROMPT_TOO_LARGE)
        request_body: dict[str, Any] = {
            "model": self.model_id, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "options": {"temperature": TEMPERATURE, "seed": SEED, "num_ctx": CONTEXT_TOKENS,
                        "num_predict": self._max_output_tokens},
        }
        if json_schema is not None:
            request_body["format"] = json_schema
        body = json.dumps(request_body).encode("utf-8")
        request = urllib.request.Request(self._url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise self._failed(LLMFailure.MODEL_UNAVAILABLE if exc.code == 404 else LLMFailure.PROVIDER_FAILURE,
                               exc) from None
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
            raise self._failed(LLMFailure.TIMEOUT if timed_out else LLMFailure.UNAVAILABLE, exc) from None
        except (TimeoutError, socket.timeout) as exc:
            raise self._failed(LLMFailure.TIMEOUT, exc) from None
        except (ValueError, OSError) as exc:  # invalid JSON, or the connection broke mid-response
            raise self._failed(LLMFailure.INVALID_RESPONSE, exc) from None

        message = payload.get("message") if isinstance(payload, dict) else None
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise self._failed(LLMFailure.INVALID_RESPONSE, None)
        if not text.strip():
            raise self._failed(LLMFailure.EMPTY_RESPONSE, None)
        tokens = payload.get("eval_count")
        return LLMOutput(text=text.strip(), output_tokens=tokens if isinstance(tokens, int) else None)

    def _failed(self, reason: LLMFailure, exc: BaseException | None) -> LLMError:
        logger.warning("llm call failed model=%s reason=%s error=%s", self.model_id, reason,
                       type(exc).__name__ if exc else "none")
        return LLMError(reason)
