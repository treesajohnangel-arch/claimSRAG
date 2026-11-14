"""Unit tests for the local LLM provider and its configuration (Phase 8), with no server: the HTTP opener is replaced
by a recorder. tests/unit/test_llm_model.py calls the real local Ollama server (marker ``llm``)."""

import io
import json
import socket
import urllib.error
import urllib.request
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm import CONTEXT_TOKENS, MAX_PROMPT_CHARS, SEED, TEMPERATURE, LLMError, LLMFailure, OllamaProvider

SECRET_TEXT = "Policy POL-77: sum insured ₹5,00,000"


class _Opener:
    def __init__(self, reply: Any = None, error: BaseException | None = None) -> None:
        self.reply, self.error, self.requests = reply, error, []

    def open(self, request: urllib.request.Request, timeout: float) -> Any:
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        body = self.reply if isinstance(self.reply, bytes) else json.dumps(self.reply).encode()
        return io.BytesIO(body)


def _provider(opener: _Opener, **settings: Any) -> OllamaProvider:
    provider = OllamaProvider(Settings(_env_file=None, **settings))
    provider._opener = opener
    return provider


def test_the_request_is_a_deterministic_non_streamed_chat_call_to_the_configured_model() -> None:
    opener = _Opener({"message": {"role": "assistant", "content": "  Covered after 36 months.  "}, "eval_count": 7})

    output = _provider(opener, llm_max_output_tokens=123, llm_timeout_seconds=42).generate("RULES", "QUESTION")

    (request, timeout), = opener.requests
    body = json.loads(request.data)
    assert (request.full_url, request.get_method(), timeout) == ("http://127.0.0.1:11434/api/chat", "POST", 42)
    assert body == {"model": "qwen2.5:3b", "stream": False,
                    "messages": [{"role": "system", "content": "RULES"}, {"role": "user", "content": "QUESTION"}],
                    "options": {"temperature": TEMPERATURE, "seed": SEED, "num_ctx": CONTEXT_TOKENS,
                                "num_predict": 123}}
    assert (TEMPERATURE, output.text, output.output_tokens) == (0.0, "Covered after 36 months.", 7)


def test_a_json_schema_constrains_the_output_format_and_changes_nothing_else() -> None:
    # Phase 9: the cited-answer verdict is requested as JSON of a fixed, server-side schema.
    schema = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}
    plain, constrained = _Opener({"message": {"content": "x"}}), _Opener({"message": {"content": "{}"}})

    _provider(plain).generate("RULES", "QUESTION")
    _provider(constrained).generate("RULES", "QUESTION", json_schema=schema)

    plain_body, constrained_body = (json.loads(opener.requests[0][0].data) for opener in (plain, constrained))
    assert "format" not in plain_body
    assert constrained_body == plain_body | {"format": schema}


@pytest.mark.security
def test_requests_ignore_environment_proxies_and_never_follow_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")

    def proxies(opener: urllib.request.OpenerDirector) -> list[Any]:
        return [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]

    handlers = OllamaProvider(Settings(_env_file=None))._opener.handlers
    redirect = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]

    assert proxies(urllib.request.build_opener()) != []  # positive control: a default opener would use the proxy
    assert proxies(OllamaProvider(Settings(_env_file=None))._opener) == []
    assert len(redirect) == 1 and redirect[0].redirect_request(None, None, 302, "Found", {}, "http://evil.example/") is None


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://127.0.0.1:11434/api/chat", code, "error", None,  # type: ignore[arg-type]
                                  io.BytesIO(f"server says: {SECRET_TEXT}".encode()))


@pytest.mark.security
@pytest.mark.parametrize(
    ("opener", "reason"),
    [(_Opener(error=urllib.error.URLError(ConnectionRefusedError(111, "refused"))), LLMFailure.UNAVAILABLE),
     (_Opener(error=urllib.error.URLError(socket.timeout("timed out"))), LLMFailure.TIMEOUT),
     (_Opener(error=TimeoutError("read timed out")), LLMFailure.TIMEOUT),
     (_Opener(error=_http_error(404)), LLMFailure.MODEL_UNAVAILABLE),
     (_Opener(error=_http_error(500)), LLMFailure.PROVIDER_FAILURE),
     (_Opener(reply=b"<html>not json</html>"), LLMFailure.INVALID_RESPONSE),
     (_Opener(reply={"done": True}), LLMFailure.INVALID_RESPONSE),
     (_Opener(reply={"message": {"content": 42}}), LLMFailure.INVALID_RESPONSE),
     (_Opener(reply=["not", "an", "object"]), LLMFailure.INVALID_RESPONSE),
     (_Opener(reply={"message": {"content": "   \n"}}), LLMFailure.EMPTY_RESPONSE)],
    ids=["unavailable", "connect-timeout", "read-timeout", "model-missing", "server-error", "not-json", "no-message",
         "content-not-text", "not-an-object", "empty-answer"],
)
def test_failures_are_typed_and_never_carry_the_prompt_or_server_text(opener: _Opener, reason: LLMFailure) -> None:
    with pytest.raises(LLMError) as excinfo:
        _provider(opener).generate("RULES", SECRET_TEXT)

    assert excinfo.value.reason is reason
    assert SECRET_TEXT not in str(excinfo.value) and "server says" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_an_oversized_prompt_is_refused_before_any_request() -> None:
    opener = _Opener({"message": {"content": "x"}})

    with pytest.raises(LLMError) as excinfo:
        _provider(opener).generate("s", "x" * MAX_PROMPT_CHARS)

    assert excinfo.value.reason is LLMFailure.PROMPT_TOO_LARGE and opener.requests == []


# --- Configuration ---------------------------------------------------------------------------------------------


def test_llm_settings_defaults(settings: Settings) -> None:
    assert (settings.ollama_base_url, settings.llm_model, settings.llm_timeout_seconds,
            settings.llm_max_output_tokens) == ("http://127.0.0.1:11434", "qwen2.5:3b", 120, 400)


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434",
                                 "http://ollama:11434", "https://host.docker.internal:11434"])
def test_local_llm_urls_are_accepted(url: str) -> None:
    assert Settings(_env_file=None, ollama_base_url=url).ollama_base_url == url.rstrip("/")


@pytest.mark.security
@pytest.mark.parametrize("url", ["https://api.openai.com", "http://10.0.0.5:11434", "http://192.168.1.20:11434",
                                 "http://ollama.example.com:11434", "http://user:secret@localhost:11434",
                                 "http://localhost:11434/v1", "http://localhost:11434?x=1", "ftp://localhost",
                                 "localhost:11434"],
                         ids=["public-api", "private-ip", "lan-ip", "remote-name", "credentials", "path", "query",
                              "other-scheme", "no-scheme"])
def test_non_local_or_unusual_llm_urls_are_refused(url: str) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, ollama_base_url=url)

    assert "secret" not in str(excinfo.value)


@pytest.mark.parametrize("model", ["", " qwen2.5:3b", "qwen2.5:3b; rm -rf /", "a" * 101, "../etc/passwd"])
def test_unusual_model_names_are_refused(model: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_model=model)
