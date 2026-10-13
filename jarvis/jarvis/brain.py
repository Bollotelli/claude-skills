"""Jarvis' brain: one Claude conversation with streaming output and tool use.

Design notes
- Streaming: text deltas go straight to the speaker, so Jarvis starts talking
  while Claude is still writing.
- Append-only history: the system prompt and tool list are frozen per
  conversation, the current time arrives as an appended system message, and a
  failed turn is rolled back by cutting only the unfinished tail. This keeps
  the prompt cache warm and preserved-thinking signatures valid.
- Refusal fallbacks are opted in (``fallbacks="default"``): if a safety
  classifier declines a request, the API retries on a recommended model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

import anthropic

from . import timefmt
from .config import Settings
from .persona import build_system_prompt
from .tools import Toolbox

# Capability tables by exact model id.
SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
SYSTEM_MESSAGE_MODELS = {
    "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-fable-5", "claude-fable-5-1",
    "claude-mythos-5", "claude-mythos-5-1", "claude-sonnet-5-5", "claude-haiku-5-5",
}
DYNAMIC_WEB_SEARCH_MODELS = {
    "claude-opus-5-5", "claude-opus-5", "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
}

# USD per million tokens: input, output, cache write (5 min), cache read.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 5.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 2.50, 0.20),
}
WEB_SEARCH_PRICE = 0.01  # USD per search

ToolCallback = Callable[[str, bool], None]  # (tool name, is slow)


class Refusal(Exception):
    """Claude declined the request (stop_reason "refusal")."""


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    searches: int = 0

    def add(self, usage: Any) -> None:
        self.input += usage.input_tokens or 0
        self.output += usage.output_tokens or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        server = getattr(usage, "server_tool_use", None)
        self.searches += getattr(server, "web_search_requests", 0) or 0

    def cost(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        p_in, p_out, p_write, p_read = PRICES[model]
        tokens = self.input * p_in + self.output * p_out + self.cache_write * p_write + self.cache_read * p_read
        return tokens / 1_000_000 + self.searches * WEB_SEARCH_PRICE


_PAIRED_RESULTS = {"web_search_tool_result", "web_fetch_tool_result"}


def echo_content(blocks: list) -> list:
    """The assistant content to append to the history.

    After a server-side fallback switched models mid-answer, blocks before the
    last ``fallback`` boundary that the new model can't continue from are
    dropped: thinking, tool_use, unpaired server_tool_use and unknown types.
    Without a fallback everything is passed back unchanged.
    """
    boundary = max((i for i, b in enumerate(blocks) if b.type == "fallback"), default=-1)
    if boundary < 0:
        return list(blocks)
    answered = {getattr(b, "tool_use_id", None) for b in blocks[:boundary] if b.type in _PAIRED_RESULTS}
    kept = []
    for i, block in enumerate(blocks):
        if i < boundary:
            if block.type == "server_tool_use":
                if block.id not in answered:
                    continue
            elif block.type not in {"text", "fallback"} | _PAIRED_RESULTS:
                continue
        kept.append(block)
    return kept


class Brain:
    MAX_ROUNDS = 12  # model calls per user turn (tool rounds and pause_turn resumes)

    def __init__(self, settings: Settings, toolbox: Toolbox, client: anthropic.Anthropic | None = None):
        self.settings = settings
        self.toolbox = toolbox
        self.client = client or anthropic.Anthropic()
        self.usage = Usage()
        self.messages: list[dict] = []
        self.system = ""
        self.tools: list[dict] = []
        self._last_turn = 0.0
        self.new_conversation()

    def new_conversation(self) -> None:
        """Start fresh; freezes system prompt (with current memories) and tools."""
        self.messages = []
        self.system = build_system_prompt(self.settings, self.toolbox.memory.all())
        self.tools = self.toolbox.definitions() + self._server_tools()

    def _server_tools(self) -> list[dict]:
        if not self.settings.web_search:
            return []
        dynamic = self.settings.model in DYNAMIC_WEB_SEARCH_MODELS
        return [{
            "type": "web_search_20260209" if dynamic else "web_search_20250305",
            "name": "web_search",
            "max_uses": 5,
            "user_location": {
                "type": "approximate",
                "city": self.settings.location,
                "country": self.settings.country,
                "timezone": self.settings.timezone,
            },
        }]

    def respond(self, text: str, on_text: Callable[[str], None], on_tool: ToolCallback | None = None) -> str:
        """Answer one user message; streams text through ``on_text`` and returns the full reply."""
        idle = time.monotonic() - self._last_turn
        if self.messages and idle > self.settings.idle_reset_minutes * 60:
            self.new_conversation()
        checkpoint = len(self.messages)
        self._append_user(text)
        try:
            reply = self._run(on_text, on_tool)
        except BaseException:
            # Cut only the unfinished tail: the history stays append-only.
            del self.messages[checkpoint:]
            raise
        self._last_turn = time.monotonic()
        return reply

    def _append_user(self, text: str) -> None:
        note = timefmt.context_note(self.settings.now(), self.settings.timezone)
        if self.settings.model in SYSTEM_MESSAGE_MODELS:
            self.messages.append({"role": "user", "content": text})
            self.messages.append({"role": "system", "content": note})
        else:
            self.messages.append({"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "text", "text": f"[{note}]"},
            ]})

    def _request(self) -> dict:
        params: dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": self.settings.max_tokens,
            "system": self.system,
            "messages": list(self.messages),
            "tools": self.tools,
            "cache_control": {"type": "ephemeral"},
            "output_config": {"effort": self.settings.effort},
        }
        if self.settings.model in SERVER_FALLBACK_MODELS:
            params["betas"] = ["server-side-fallback-2026-07-01"]
            params["fallbacks"] = "default"
        return params

    def _stream_once(self, parts: list[str], on_text: Callable[[str], None], on_tool: ToolCallback | None):
        with self.client.beta.messages.stream(**self._request()) as stream:
            for event in stream:
                if event.type == "text":
                    parts.append(event.text)
                    on_text(event.text)
                elif event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "text" and parts and not parts[-1][-1:].isspace():
                        parts.append(" ")  # keep sentences of separate text blocks apart
                        on_text(" ")
                    elif on_tool and block.type == "tool_use":
                        on_tool(block.name, self.toolbox.is_slow(block.name))
                    elif on_tool and block.type == "server_tool_use":
                        on_tool(block.name, True)
            return stream.get_final_message()

    def _run(self, on_text: Callable[[str], None], on_tool: ToolCallback | None) -> str:
        parts: list[str] = []
        for _ in range(self.MAX_ROUNDS):
            response = self._stream_once(parts, on_text, on_tool)
            self.usage.add(response.usage)

            if response.stop_reason == "refusal":
                raise Refusal(getattr(response.stop_details, "category", None))
            content = echo_content(response.content)
            self.messages.append({"role": "assistant", "content": content})
            if response.stop_reason == "pause_turn":
                continue  # a server-side tool loop paused; re-sending resumes it

            tool_uses = [b for b in content if b.type == "tool_use"]
            if not tool_uses:
                return "".join(parts)
            results = []
            for block in tool_uses:
                if response.stop_reason == "max_tokens":
                    # A cut-off input can still parse as a valid partial object.
                    results.append({
                        "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                        "content": "Eingabe abgeschnitten (max_tokens erreicht). Bitte kürzer fassen.",
                    })
                else:
                    results.append(self.toolbox.run(block.name, block.id, block.input))
            self.messages.append({"role": "user", "content": results})
        return "".join(parts)
