"""The model client: one Messages API call per turn, as plain dicts.

``ModelClient`` is all the runner needs, so tests pass a scripted fake and never reach the
network. ``AnthropicClient`` is the real one (the ``anthropic`` SDK, streaming so long turns
don't hit HTTP timeouts). Responses come back as dicts (``content`` blocks, ``stop_reason``,
``usage``); the runner appends ``content`` unchanged, thinking blocks included, as the API
asks.
"""

from __future__ import annotations

from typing import Any, Protocol

NO_TOOLS = "this model cannot use tools; the coach needs a model that can (pick another)"


def capabilities(provider: str, model: str) -> set[str]:
    """What a model can do, as far as the harness knows before calling it. Every provider
    here speaks tool use; a local model that cannot is caught on its first reply
    (``NO_TOOLS``)."""
    return {"tool_use"}


class ModelError(Exception):
    """A call failed. ``retry`` says whether trying again later can help."""

    def __init__(self, message: str, *, retry: bool) -> None:
        super().__init__(message)
        self.retry = retry


class ModelClient(Protocol):
    def create(self, request: dict[str, Any]) -> dict[str, Any]: ...


class AnthropicClient:
    def __init__(self, api_key: str) -> None:
        import anthropic

        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=600.0)

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        sdk = self._sdk
        try:
            with self._client.messages.stream(**request) as stream:
                message = stream.get_final_message()
        except (sdk.AuthenticationError, sdk.PermissionDeniedError) as exc:
            raise ModelError(f"the API key was refused ({exc.status_code})", retry=False) from exc
        except sdk.NotFoundError as exc:
            raise ModelError("the model or endpoint was not found (404)", retry=False) from exc
        except sdk.RateLimitError as exc:
            raise ModelError("rate limited by the API (429)", retry=True) from exc
        except sdk.BadRequestError as exc:
            message = f"the API refused the request (400): {exc.message}"
            raise ModelError(message, retry=False) from exc
        except sdk.APIStatusError as exc:
            retry = exc.status_code >= 500 or exc.status_code in (408, 409)
            raise ModelError(f"API error {exc.status_code}", retry=retry) from exc
        except sdk.APIConnectionError as exc:
            raise ModelError("could not reach the API", retry=True) from exc
        return message.to_dict()


class OpenAICompatClient:
    """OpenAI's Chat Completions format: OpenAI, xAI and local servers (Ollama, LM Studio,
    vLLM) speak it. The runner keeps talking in Anthropic's Messages shape; this client
    translates the request (system, messages with tool_use / tool_result, tools) and the
    answer (text, tool_calls, finish_reason, usage) both ways. Thinking settings, cache hints
    and thinking blocks have no counterpart and are left out."""

    def __init__(self, base_url: str, api_key: str | None, *, http=None) -> None:
        import requests

        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._http = http or requests.post
        self._requests = requests

    @staticmethod
    def to_openai(request: dict[str, Any]) -> dict[str, Any]:
        import json

        system = request.get("system") or ""
        if isinstance(system, list):
            system = "\n\n".join(b.get("text", "") for b in system if b.get("type") == "text")
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
        for message in request.get("messages") or []:
            content = message.get("content")
            if isinstance(content, str):
                messages.append({"role": message["role"], "content": content})
                continue
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if message["role"] == "assistant":
                calls = [
                    {
                        "id": b["id"],
                        "type": "function",
                        "function": {
                            "name": b["name"],
                            "arguments": json.dumps(b.get("input") or {}),
                        },
                    }
                    for b in content
                    if b.get("type") == "tool_use"
                ]
                out: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
                if calls:
                    out["tool_calls"] = calls
                messages.append(out)
                continue
            for b in content:
                if b.get("type") == "tool_result":
                    value = b.get("content")
                    if isinstance(value, list):
                        value = "\n".join(x.get("text", "") for x in value if isinstance(x, dict))
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": b["tool_use_id"],
                            "content": str(value or ""),
                        }
                    )
            if texts:
                messages.append({"role": "user", "content": "\n".join(texts)})
        body: dict[str, Any] = {"model": request["model"], "messages": messages}
        if request.get("tools"):
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema") or {"type": "object"},
                    },
                }
                for t in request["tools"]
            ]
        if request.get("max_tokens"):
            body["max_tokens"] = request["max_tokens"]
        return body

    @staticmethod
    def from_openai(data: dict[str, Any]) -> dict[str, Any]:
        import json

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content: list[dict[str, Any]] = []
        if message.get("content"):
            content.append({"type": "text", "text": message["content"]})
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            content.append(
                {
                    "type": "tool_use",
                    "id": call.get("id"),
                    "name": fn.get("name"),
                    "input": arguments,
                }
            )
        finish = choice.get("finish_reason")
        stop = {"tool_calls": "tool_use", "length": "max_tokens"}.get(finish or "", "end_turn")
        usage = data.get("usage") or {}
        return {
            "role": "assistant",
            "content": content,
            "stop_reason": stop,
            "usage": {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
            },
        }

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        try:
            response = self._http(
                self._url, json=self.to_openai(request), headers=headers, timeout=600
            )
        except self._requests.RequestException as exc:
            raise ModelError("could not reach the API", retry=True) from exc
        status = getattr(response, "status_code", 200)
        if status in (401, 403):
            raise ModelError(f"the API key was refused ({status})", retry=False)
        if status == 404:
            raise ModelError("the model or endpoint was not found (404)", retry=False)
        if status == 429:
            raise ModelError("rate limited by the API (429)", retry=True)
        if status >= 400:
            text = getattr(response, "text", "")[:200]
            if status == 400 and "tool" in text.lower() and "support" in text.lower():
                raise ModelError(NO_TOOLS, retry=False)
            raise ModelError(f"API error {status}: {text}", retry=status >= 500)
        try:
            return self.from_openai(response.json())
        except (ValueError, AttributeError, KeyError, TypeError, IndexError) as exc:
            # A 200 with an HTML page, an empty body or another shape (seen with local servers).
            raise ModelError(
                f"the API answered in a form that could not be read ({type(exc).__name__})",
                retry=True,
            ) from exc
