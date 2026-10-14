"""Free brain: an open model running locally through Ollama (https://ollama.com).

No API key, no costs, and the conversation never leaves the computer. Needs
the Ollama app running and a model pulled once (``ollama pull qwen3:8b``).
Same interface as ``brain.Brain``, so the rest of Jarvis does not care which
brain is thinking.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

from . import timefmt
from .config import Settings
from .persona import build_system_prompt
from .tools import Toolbox

# Families with a reasoning mode that ``think=False`` switches off (faster answers).
THINKING_FAMILIES = ("qwen3", "deepseek-r1", "magistral")
TOOL_RESULT_CHARS = 6000  # keep tool results within the local context window

ToolCallback = Callable[[str, bool], None]


class LocalModelError(RuntimeError):
    """Ollama is not reachable or the model is missing; the message says how to fix it."""


@dataclass
class LocalUsage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    searches: int = 0

    def cost(self, model: str) -> float:
        return 0.0  # runs on your own computer


class ThinkFilter:
    """Drops ``<think>…</think>`` sections that some models write into their answer."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.buffer = ""

    def feed(self, text: str) -> str:
        self.buffer += text
        out = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            pos = self.buffer.find(tag)
            if pos == -1:
                # keep a possible partial tag at the end for the next delta
                keep = len(tag) - 1
                if not self.inside and len(self.buffer) > keep:
                    out.append(self.buffer[:-keep] if keep else self.buffer)
                    self.buffer = self.buffer[-keep:] if keep else ""
                elif self.inside:
                    self.buffer = self.buffer[-keep:]
                break
            if not self.inside:
                out.append(self.buffer[:pos])
            self.buffer = self.buffer[pos + len(tag):]
            self.inside = not self.inside
        return "".join(out)

    def flush(self) -> str:
        rest, self.buffer = ("" if self.inside else self.buffer), ""
        return rest


_ANY_ADDRESS = ("", "0.0.0.0", "::")  # server bind addresses (Ollama FAQ), not reachable as a client


def normalize_host(host: str) -> str:
    """Turn OLLAMA_HOST values meant for the server (0.0.0.0, :11434) into a client address."""
    if not host:
        return ""
    parsed = urlparse(host if "://" in host else f"http://{host}")
    if (parsed.hostname or "") in _ANY_ADDRESS:
        return f"{parsed.scheme}://127.0.0.1:{parsed.port or 11434}"
    return host


def _is_local_host(host: str) -> bool:
    host = normalize_host(host)
    if not host:
        return True
    name = urlparse(host if "://" in host else f"http://{host}").hostname or ""
    return name in ("localhost", "127.0.0.1", "::1")


class LocalBrain:
    MAX_ROUNDS = 8  # model calls per user turn

    def __init__(self, settings: Settings, toolbox: Toolbox, client: Any = None):
        self.settings = settings
        self.toolbox = toolbox
        if client is None:
            try:
                import ollama
            except ImportError as exc:
                raise LocalModelError("Das Paket 'ollama' fehlt: pip install -r requirements.txt") from exc
            # Never send local traffic through a system proxy.
            extra = {"trust_env": False} if _is_local_host(settings.ollama_host) else {}
            client = ollama.Client(host=normalize_host(settings.ollama_host) or None, **extra)
        self.client = client
        self.usage = LocalUsage()
        self.max_history = max(12, settings.ollama_ctx // 512)  # 24 messages at the default 12288
        self.messages: list[dict] = []
        self.system = ""
        self.tools: list[dict] = []
        self._think: bool | None = False if settings.ollama_model.lower().startswith(THINKING_FAMILIES) else None
        self._last_turn = 0.0
        self.new_conversation()

    @property
    def label(self) -> str:
        return f"{self.settings.ollama_model} (lokal, kostenlos)"

    def new_conversation(self) -> None:
        self.messages = []
        self.system = build_system_prompt(self.settings, self.toolbox.memory.all(), local=True)
        self.tools = self.toolbox.function_definitions()

    def respond(self, text: str, on_text: Callable[[str], None], on_tool: ToolCallback | None = None) -> str:
        idle = time.monotonic() - self._last_turn
        if self.messages and idle > self.settings.idle_reset_minutes * 60:
            self.new_conversation()
        checkpoint = len(self.messages)
        note = timefmt.context_note(self.settings.now(), self.settings.timezone)
        self.messages.append({"role": "user", "content": f"[{note}]\n{text}"})
        try:
            reply = self._run(on_text, on_tool)
        except BaseException:
            del self.messages[checkpoint:]
            raise
        self._trim()
        self._last_turn = time.monotonic()
        return reply

    def _trim(self) -> None:
        """Forget the oldest exchanges once the history is full.

        Cuts down to half at a user-message boundary, so the start of the
        history (and Ollama's cached prompt) changes only every few turns,
        and never cuts into the exchange that was just answered.
        """
        if len(self.messages) <= self.max_history:
            return
        last_user = max(i for i, m in enumerate(self.messages) if m["role"] == "user")
        cut = len(self.messages) - self.max_history // 2
        while cut < last_user and self.messages[cut]["role"] != "user":
            cut += 1
        del self.messages[:min(cut, last_user)]

    def _chat(self, with_tools: bool = True):
        kwargs: dict[str, Any] = {
            "model": self.settings.ollama_model,
            "messages": [{"role": "system", "content": self.system}, *self.messages],
            "tools": self.tools if with_tools else [],
            "stream": True,
            "options": {"num_ctx": self.settings.ollama_ctx, "temperature": 0.6},
            "keep_alive": "30m",
        }
        if self._think is not None:
            kwargs["think"] = self._think
        return self.client.chat(**kwargs)

    def _stream_once(
        self, parts: list[str], on_text: Callable[[str], None], with_tools: bool = True
    ) -> tuple[str, list[dict]]:
        import httpx  # transport of the ollama client
        import ollama

        content: list[str] = []
        calls: list[dict] = []
        think = ThinkFilter()

        def emit(piece: str) -> None:
            if not piece:
                return
            if not content:
                piece = piece.lstrip()  # models often open with blank lines
                if not piece:
                    return
                if parts and not parts[-1][-1:].isspace():
                    parts.append(" ")
                    on_text(" ")
            content.append(piece)
            parts.append(piece)
            on_text(piece)

        try:
            for chunk in self._chat(with_tools):
                message = chunk.message
                if message.content:
                    emit(think.feed(message.content))
                for call in message.tool_calls or []:
                    calls.append({"name": call.function.name, "arguments": dict(call.function.arguments or {})})
                if chunk.done:
                    self.usage.input += chunk.prompt_eval_count or 0
                    self.usage.output += chunk.eval_count or 0
            emit(think.flush())
        except ollama.ResponseError as exc:
            if self._think is not None and "think" in str(exc.error).lower() and not content:
                self._think = None  # this model has no switchable reasoning: ask again without it
                return self._stream_once(parts, on_text, with_tools)
            if "does not support tools" in str(exc.error).lower():
                raise LocalModelError(
                    f"Das Modell '{self.settings.ollama_model}' kann keine Werkzeuge nutzen. "
                    "Nimm ein Modell mit Werkzeug-Unterstützung, z. B. qwen3:4b oder qwen3:8b."
                ) from exc
            if exc.status_code == 404:
                raise LocalModelError(
                    f"Das Modell '{self.settings.ollama_model}' ist nicht installiert. "
                    f"Lade es einmalig mit: ollama pull {self.settings.ollama_model}"
                ) from exc
            raise LocalModelError(f"Ollama meldet einen Fehler: {exc.error}") from exc
        except (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise LocalModelError(
                "Ollama läuft nicht. Starte die Ollama-App (oder 'ollama serve') und versuche es erneut."
            ) from exc
        return "".join(content), calls

    def _run(self, on_text: Callable[[str], None], on_tool: ToolCallback | None) -> str:
        parts: list[str] = []
        for _ in range(self.MAX_ROUNDS):
            content, calls = self._stream_once(parts, on_text)
            assistant: dict[str, Any] = {"role": "assistant", "content": content}
            if calls:
                assistant["tool_calls"] = [{"function": call} for call in calls]
            self.messages.append(assistant)
            if not calls:
                return "".join(parts)
            for call in calls:
                if on_tool:
                    on_tool(call["name"], self.toolbox.is_slow(call["name"]))
                result = self.toolbox.run(call["name"], "local", call["arguments"])
                text = str(result["content"])
                if len(text) > TOOL_RESULT_CHARS:
                    text = text[:TOOL_RESULT_CHARS] + f"\n[... gekürzt, insgesamt {len(text)} Zeichen]"
                if result.get("is_error"):
                    text = f"FEHLER: {text}"
                self.messages.append({"role": "tool", "tool_name": call["name"], "content": text})
        # Out of tool rounds (e.g. a small model retrying a failing call): one last
        # turn without tools, so there is always a spoken answer.
        content, _ = self._stream_once(parts, on_text, with_tools=False)
        self.messages.append({"role": "assistant", "content": content})
        return "".join(parts)
