"""``python -m jarvis selbsttest``: offline checks, no API key, no costs.

Covers the tools, the sentence chunker and a full conversation turn through
the real Anthropic SDK against a local fake of the streaming Messages API.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import traceback
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np


class FakeMessagesAPI:
    """Serves scripted SSE responses on 127.0.0.1 and records every request."""

    def __init__(self, responses: list[list[dict]]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (http.server naming)
                length = int(self.headers.get("content-length", 0))
                api.requests.append({"headers": dict(self.headers), "body": json.loads(self.rfile.read(length))})
                events = api.responses.pop(0)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for event in events:
                    self.wfile.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()


def sse_message(blocks: list[dict], stop_reason: str) -> list[dict]:
    """Build the SSE events of one assistant message."""
    events = [{"type": "message_start", "message": {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1},
    }}]
    for i, block in enumerate(blocks):
        if block["type"] == "thinking":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
            events.append({"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "sig-test"}})
        elif block["type"] == "text":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
            for word in block["text"].split(" "):
                events.append({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": word + " "}})
        elif block["type"] == "tool_use":
            events.append({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}})
            raw = json.dumps(block["input"], ensure_ascii=False)
            for part in (raw[: len(raw) // 2], raw[len(raw) // 2:]):
                events.append({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": part}})
        events.append({"type": "content_block_stop", "index": i})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 42}})
    events.append({"type": "message_stop"})
    return events


# --- individual checks ------------------------------------------------------

def check_chunker() -> None:
    from .speech import SentenceChunker, clean_for_speech

    chunker = SentenceChunker()
    text = "Am 13. Oktober um 14 Uhr, z. B. im Büro. Danach Pause! Okay?"
    sentences = []
    for char in text:
        sentences += chunker.feed(char)
    sentences += chunker.flush()
    assert sentences == ["Am 13. Oktober um 14 Uhr, z. B. im Büro.", "Danach Pause! Okay?"], sentences
    cleaned = clean_for_speech("**Wichtig:** siehe https://example.org 🙂\n- Punkt eins")
    assert cleaned == "Wichtig: siehe Punkt eins", cleaned


def check_validation() -> None:
    from .tools import validate

    schema = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}, "required": ["n"], "additionalProperties": False}
    assert validate({"n": 2}, schema) is None
    assert validate({}, schema)
    assert validate({"n": "2"}, schema)
    assert validate({"n": 0}, schema)
    assert validate({"n": 2, "x": 1}, schema)
    assert validate({"n": True}, schema)


def check_tools(data_dir: Path) -> None:
    from .config import Settings
    from .tools import Toolbox

    settings = Settings(data_dir=data_dir)
    toolbox = Toolbox(settings)

    def run(name: str, args: dict) -> dict:
        return toolbox.run(name, "toolu_test", args)

    assert "#1" in run("add_task", {"title": "Steuerberater anrufen"})["content"]
    assert "Steuerberater" in run("list_tasks", {})["content"]
    assert run("complete_task", {"task_id": 1}).get("is_error") is None
    assert run("complete_task", {"task_id": 7})["is_error"]
    assert run("add_task", {"titel": "falsch"})["is_error"]
    run("save_note", {"title": "Business-Idee", "content": "Kaffee-Abo für Büros"})
    assert "Kaffee" in run("search_notes", {"query": "büros"})["content"]
    assert "Kaffee" in run("read_note", {"note": "Business-Idee"})["content"]
    run("remember", {"fact": "Mein Gegenüber heißt Alex."})
    assert toolbox.memory.all()[0]["fact"] == "Mein Gegenüber heißt Alex."
    assert run("set_reminder", {"text": "Tee", "in_minutes": 5}).get("is_error") is None
    assert run("set_reminder", {"text": "Tee", "in_minutes": 5, "at": "2030-01-01T09:00:00"})["is_error"]
    assert run("set_reminder", {"text": "alt", "at": "2001-01-01T09:00:00"})["is_error"]
    assert run("read_skill_file", {"path": "../../etc/passwd"})["is_error"]
    assert run("open_website", {"url": "file:///etc/passwd"})["is_error"]

    later = datetime.now(settings.tz()) + timedelta(minutes=6)
    fired = toolbox.reminders.pop_due(later)
    assert [r["text"] for r in fired] == ["Tee"], fired
    assert toolbox.reminders.pop_due(later) == []

    # reminders sort by real time, whatever UTC offset they were stored with
    base = datetime(2031, 5, 1, 12, 0, tzinfo=timezone.utc)
    toolbox.reminders.add("zweite", base.astimezone(timezone(timedelta(hours=-5))) + timedelta(hours=1))
    toolbox.reminders.add("erste", base.astimezone(timezone(timedelta(hours=2))))
    assert [r["text"] for r in toolbox.reminders.pending()] == ["erste", "zweite"]

    # a damaged (hand-edited) file is moved aside, never overwritten
    (data_dir / "aufgaben.json").write_text('[{"id": 1, "title": "kaputt",]', encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        assert toolbox.tasks.list() == []
    assert list(data_dir.glob("aufgaben.defekt-*.json")), "damaged file was not kept"


def check_skills() -> None:
    from .config import REPO_ROOT
    from .tools.skills import SkillLibrary

    library = SkillLibrary(REPO_ROOT)
    if not library.index():
        return  # app copied out of the repository: nothing to search
    hits = library.find("pricing strategy")
    assert hits and "pricing" in hits[0]["path"], hits


def check_frontmatter() -> None:
    from .tools.skills import _frontmatter

    meta = _frontmatter('---\nname: demo\ndescription: >\n  Folded text\n  over two lines.\nversion: 1\n---\n# Demo')
    assert meta == {"name": "demo", "description": "Folded text over two lines."}, meta
    assert _frontmatter('---\nname: x\ndescription: "Quoted"\n---')["description"] == "Quoted"


def check_weather_format() -> None:
    from .tools.weather import format_weather

    today = datetime.now().date()
    data = {
        "current": {"temperature_2m": 14.2, "apparent_temperature": 12.8, "relative_humidity_2m": 71, "weather_code": 3, "wind_speed_10m": 11.0},
        "daily": {
            "time": [today.isoformat()], "weather_code": [61], "temperature_2m_min": [8.9], "temperature_2m_max": [15.4],
            "precipitation_probability_max": [60], "sunrise": [f"{today}T07:21"], "sunset": [f"{today}T18:24"],
        },
    }
    text = format_weather({"name": "Berlin", "country": "Deutschland"}, data)
    assert "Jetzt: 14 °C" in text and "Heute: leichter Regen, 9 bis 15 °C" in text, text
    data["current"]["apparent_temperature"] = None
    data["daily"]["temperature_2m_max"] = [None]
    text = format_weather({"name": "Berlin"}, data)
    assert "gefühlt ? °C" in text and "9 bis ? °C" in text, text


def check_audio_helpers() -> None:
    from .audio import AudioClip
    from .stt import clean_transcript
    from .tts import decode_audio, wav_bytes

    tone = AudioClip((0.2 * np.sin(np.linspace(0, 440 * 2 * np.pi, 24000))).astype(np.float32), 24000)
    decoded = decode_audio(wav_bytes(tone))
    assert decoded.rate == 24000 and abs(len(decoded.samples) - 24000) < 10
    assert clean_transcript("Untertitel im Auftrag des ZDF, 2018") == ""
    assert clean_transcript("Was ist eigentlich Copyright?") == "Was ist eigentlich Copyright?"


def check_echo_content() -> None:
    from .brain import echo_content

    def block(kind: str, **fields):
        return SimpleNamespace(type=kind, **fields)

    blocks = [
        block("thinking"), block("text"), block("tool_use", id="t1"),
        block("server_tool_use", id="s1"), block("web_search_tool_result", tool_use_id="s1"),
        block("server_tool_use", id="s2"), block("fallback"), block("thinking"), block("text"),
    ]
    kept = [b.type for b in echo_content(blocks)]
    assert kept == ["text", "server_tool_use", "web_search_tool_result", "fallback", "thinking", "text"], kept
    assert echo_content(blocks[:3]) == blocks[:3]


def check_conversation(data_dir: Path) -> None:
    import anthropic

    from .brain import Brain, Refusal
    from .config import Settings
    from .tools import Toolbox

    fake = FakeMessagesAPI([
        sse_message([
            {"type": "thinking"},
            {"type": "text", "text": "Sehr wohl."},
            {"type": "tool_use", "id": "toolu_1", "name": "add_task", "input": {"title": "Steuerberater anrufen"}},
        ], "tool_use"),
        sse_message([{"type": "text", "text": "Ist notiert."}], "end_turn"),
        sse_message([{"type": "text", "text": "Das kann ich"}], "refusal"),
    ])
    try:
        client = anthropic.Anthropic(
            api_key="test", base_url=fake.url, max_retries=0,
            http_client=anthropic.DefaultHttpxClient(trust_env=False),
        )
        settings = Settings(data_dir=data_dir)
        toolbox = Toolbox(settings)
        brain = Brain(settings, toolbox, client=client)
        spoken, tools_used = [], []
        reply = brain.respond("Notier bitte: Steuerberater anrufen", spoken.append, lambda n, s: tools_used.append(n))

        assert reply.split() == ["Sehr", "wohl.", "Ist", "notiert."], reply
        assert tools_used == ["add_task"], tools_used
        assert any(t["title"] == "Steuerberater anrufen" for t in toolbox.tasks.list())

        first, second = fake.requests[0], fake.requests[1]
        body = first["body"]
        assert body["model"] == "claude-opus-5-5"
        assert body["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in first["headers"].get("anthropic-beta", "")
        assert body["cache_control"] == {"type": "ephemeral"}
        assert body["output_config"] == {"effort": "low"}
        assert body["messages"][-1]["role"] == "system" and "Aktuelle Zeit" in body["messages"][-1]["content"]
        assert any(t.get("type") == "web_search_20260209" for t in body["tools"])
        assert all(set(t) == {"name", "description", "input_schema"} for t in body["tools"] if "input_schema" in t)

        history = second["body"]["messages"]
        assert [m["role"] for m in history] == ["user", "system", "assistant", "user"], history
        assistant_types = [b["type"] for b in history[2]["content"]]
        assert assistant_types == ["thinking", "text", "tool_use"], assistant_types
        assert history[2]["content"][0]["signature"] == "sig-test"
        assert history[3]["content"][0]["tool_use_id"] == "toolu_1"
        assert second["body"]["system"] == body["system"], "system prompt must stay frozen"

        before = list(brain.messages)
        try:
            brain.respond("Etwas Verbotenes", lambda _: None)
            raise AssertionError("refusal was not raised")
        except Refusal:
            pass
        assert brain.messages == before, "a refused turn must be rolled back"
    finally:
        fake.close()


CHECKS = [
    ("Satzerkennung & Sprachtext-Bereinigung", lambda d: check_chunker()),
    ("Eingabe-Validierung der Werkzeuge", lambda d: check_validation()),
    ("Aufgaben, Notizen, Gedächtnis, Erinnerungen", check_tools),
    ("Skill-Bibliothek durchsuchen", lambda d: check_skills()),
    ("Skill-Beschreibungen einlesen (YAML)", lambda d: check_frontmatter()),
    ("Wetter-Zusammenfassung", lambda d: check_weather_format()),
    ("Audio-Hilfen (WAV, Whisper-Filter)", lambda d: check_audio_helpers()),
    ("Verlauf nach Modellwechsel (Fallback)", lambda d: check_echo_content()),
    ("Gespräch mit Werkzeug über das Claude-SDK", check_conversation),
]


def main() -> int:
    from rich.console import Console

    console = Console()
    failures = 0
    for label, check in CHECKS:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                check(Path(tmp))
                console.print(f"[green]✓[/] {label}")
            except Exception:
                failures += 1
                console.print(f"[red]✗[/] {label}")
                console.print(traceback.format_exc(), style="dim", markup=False)
    if failures:
        console.print(f"\n[red]{failures} Test(s) fehlgeschlagen.[/]")
        return 1
    console.print("\n[green]Alle Selbsttests bestanden.[/]")
    return 0
