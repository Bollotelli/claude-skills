"""Jarvis' abilities: tool definitions for Claude and their local implementations.

Each tool is a JSON schema (what Claude sees) plus a Python handler (what
runs on this computer). Adding a new ability = adding one ``Tool`` entry in
``Toolbox._build``.
"""

from __future__ import annotations

import json
import webbrowser
from dataclasses import dataclass
from datetime import timedelta, timezone
from typing import Any, Callable

from .. import timefmt
from ..config import Settings
from .memory import Memory
from .notes import Notes
from .reminders import Reminders, parse_time
from .skills import SkillLibrary
from .tasks import TaskList
from .trading import TradeBook
from .weather import Weather
from .web import WebError, WebSearch


class ToolError(Exception):
    """A problem Claude should hear about (returned as an error tool_result)."""


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], str]
    slow: bool = False  # Jarvis says a short filler line while it runs
    web: bool = False  # free web tool, only for the local brain (Claude searches server-side)


def _obj(properties: dict, required: tuple[str, ...] = ()) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


def validate(value: Any, schema: dict, path: str = "input") -> str | None:
    """Check ``value`` against the small JSON-schema subset used here.

    Defense in depth: every input is checked before its handler runs.
    Returns a description of the problem, or None.
    """
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{path} muss ein Objekt sein"
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key} fehlt"
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                return f"unbekannte Felder in {path}: {', '.join(extra)}"
        for key, sub in props.items():
            if key in value:
                problem = validate(value[key], sub, f"{path}.{key}")
                if problem:
                    return problem
        return None
    check = _TYPE_CHECKS.get(kind)
    if check and not check(value):
        return f"{path} muss vom Typ {kind} sein"
    if kind == "string" and schema.get("minLength", 0) > len(value.strip()):
        return f"{path} darf nicht leer sein"
    if kind in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} muss >= {schema['minimum']} sein"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} muss <= {schema['maximum']} sein"
    return None


def coerce(args: Any, schema: dict) -> Any:
    """Repair small slips before validating: numeric strings, out-of-range numbers, unknown keys.

    Open models see only types (the ollama client drops limits like minimum or
    additionalProperties), so they are corrected here instead of rejected.
    """
    if not isinstance(args, dict) or schema.get("type") != "object":
        return args
    props = schema.get("properties", {})
    fixed = {}
    for key, value in args.items():
        sub = props.get(key)
        if sub is None:
            continue  # unknown key
        kind = sub.get("type")
        if kind in ("integer", "number") and isinstance(value, str):
            try:
                value = int(value) if kind == "integer" else float(value)
            except ValueError:
                pass
        if kind == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)
        if kind in ("integer", "number") and isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in sub:
                value = max(value, sub["minimum"])
            if "maximum" in sub:
                value = min(value, sub["maximum"])
        fixed[key] = value
    return fixed


class Toolbox:
    def __init__(self, settings: Settings):
        self.settings = settings
        data = settings.data_dir
        self.tasks = TaskList(data / "aufgaben.json")
        self.notes = Notes(data / "notizen")
        self.memory = Memory(data / "gedaechtnis.json")
        self.reminders = Reminders(data / "erinnerungen.json", settings.tz())
        self.skills = SkillLibrary(settings.skills_dir)
        self.weather = Weather(settings.location)
        self.trades = TradeBook(data / "trading.json")
        self.web = WebSearch(f"{settings.country.lower()}-{settings.language.lower()}")
        self._tools = {tool.name: tool for tool in self._build()}

    # -- what Claude sees ---------------------------------------------------

    def definitions(self, include_web: bool = False) -> list[dict]:
        """Tool definitions in Claude's format.

        No eager_input_streaming: tool inputs are never shown or spoken, and
        buffered inputs arrive validated, so a malformed call can't force a
        re-issued turn that would repeat sentences already spoken aloud.
        """
        return [
            {"name": tool.name, "description": tool.description, "input_schema": tool.schema}
            for tool in self._tools.values()
            if include_web or not tool.web
        ]

    def function_definitions(self) -> list[dict]:
        """The same tools in the function-calling format of Ollama / open models."""
        return [
            {"type": "function", "function": {"name": d["name"], "description": d["description"], "parameters": d["input_schema"]}}
            for d in self.definitions(include_web=True)
        ]

    def is_slow(self, name: str) -> bool:
        tool = self._tools.get(name)
        return bool(tool and tool.slow)

    def run(self, name: str, tool_use_id: str, args: Any) -> dict:
        """Validate and execute one tool call; always returns a tool_result block."""
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_use_id}
        tool = self._tools.get(name)
        if tool is None:
            return {**result, "is_error": True, "content": f"Unbekanntes Werkzeug: {name}"}
        args = coerce(args, tool.schema)
        problem = validate(args, tool.schema)
        if problem:
            payload = {"INVALID_JSON": json.dumps(args, ensure_ascii=False), "problem": problem}
            return {**result, "is_error": True, "content": json.dumps(payload, ensure_ascii=False)}
        try:
            return {**result, "content": tool.handler(args)}
        except ToolError as exc:
            return {**result, "is_error": True, "content": str(exc)}
        except Exception as exc:  # network down, disk full, ...
            return {**result, "is_error": True, "content": f"Fehler in {name}: {type(exc).__name__}: {exc}"}

    # -- handlers -----------------------------------------------------------

    def _weather(self, args: dict) -> str:
        try:
            return self.weather.forecast(args.get("location", ""), args.get("days", 1))
        except LookupError as exc:
            raise ToolError(str(exc)) from exc

    def _add_task(self, args: dict) -> str:
        task = self.tasks.add(args["title"], args.get("due", ""))
        return f"Aufgabe #{task['id']} angelegt: {task['title']}" + (f" (fällig: {task['due']})" if task["due"] else "")

    def _list_tasks(self, args: dict) -> str:
        tasks = self.tasks.list(args.get("include_done", False))
        if not tasks:
            return "Die Aufgabenliste ist leer."
        lines = []
        for t in tasks:
            line = f"#{t['id']} {t['title']}"
            if t.get("due"):
                line += f" (fällig: {t['due']})"
            if t.get("done"):
                line += " [erledigt]"
            lines.append(line)
        return "\n".join(lines)

    def _complete_task(self, args: dict) -> str:
        task = self.tasks.complete(args["task_id"])
        if task is None:
            raise ToolError(f"Keine Aufgabe mit der Nummer {args['task_id']}.")
        return f"Erledigt: #{task['id']} {task['title']}"

    def _save_note(self, args: dict) -> str:
        path, appended = self.notes.save(args["title"], args["content"])
        verb = "ergänzt" if appended else "gespeichert"
        return f"Notiz {verb}: {path.stem} ({path})"

    def _search_notes(self, args: dict) -> str:
        hits = self.notes.search(args.get("query", ""))
        if not hits:
            return "Keine passenden Notizen gefunden."
        return "\n".join(f"[{h['id']}] {h['title']}: {h['snippet']}" for h in hits)

    def _read_note(self, args: dict) -> str:
        text = self.notes.read(args["note"])
        if text is None:
            raise ToolError(f"Notiz nicht gefunden: {args['note']}")
        return text

    def _remember(self, args: dict) -> str:
        entry = self.memory.remember(args["fact"])
        return f"Gespeichert als Erinnerung [{entry['id']}]: {entry['fact']}"

    def _forget(self, args: dict) -> str:
        entry = self.memory.forget(args["memory_id"])
        if entry is None:
            raise ToolError(f"Kein gespeicherter Fakt mit der Nummer {args['memory_id']}.")
        return f"Vergessen: {entry['fact']}"

    def _set_reminder(self, args: dict) -> str:
        has_minutes, has_at = "in_minutes" in args, "at" in args
        if has_minutes == has_at:
            raise ToolError("Gib genau eines an: in_minutes oder at.")
        now = self.settings.now()
        if has_minutes:  # real elapsed time, also across a daylight-saving switch
            due = (now.astimezone(timezone.utc) + timedelta(minutes=args["in_minutes"])).astimezone(now.tzinfo)
        else:
            try:
                due = parse_time(args["at"], self.settings.tz())
            except ValueError as exc:
                raise ToolError(f"Ungültige Zeitangabe: {args['at']}") from exc
            if due <= now:
                raise ToolError(f"Dieser Zeitpunkt liegt in der Vergangenheit ({timefmt.spoken(due)}).")
        item = self.reminders.add(args["text"], due)
        return f"Erinnerung #{item['id']} gesetzt für {timefmt.spoken(due.astimezone(self.settings.tz()))}: {item['text']}"

    def _list_reminders(self, args: dict) -> str:
        items = self.reminders.pending()
        if not items:
            return "Keine offenen Erinnerungen."
        tz = self.settings.tz()
        return "\n".join(
            f"#{r['id']} {timefmt.spoken(parse_time(r['due'], tz).astimezone(tz))}: {r['text']}" for r in items
        )

    def _cancel_reminder(self, args: dict) -> str:
        item = self.reminders.cancel(args["reminder_id"])
        if item is None:
            raise ToolError(f"Keine offene Erinnerung mit der Nummer {args['reminder_id']}.")
        return f"Abgesagt: #{item['id']} {item['text']}"

    def _open_website(self, args: dict) -> str:
        url = args["url"].strip()
        if not url.startswith(("https://", "http://")):
            raise ToolError("Nur http(s)-Adressen können geöffnet werden.")
        if not webbrowser.open(url):
            raise ToolError("Auf diesem Gerät konnte kein Browser geöffnet werden.")
        return f"Im Browser geöffnet: {url}"

    def _web_search(self, args: dict) -> str:
        try:
            return self.web.search(args["query"], args.get("max_results", 5))
        except WebError as exc:
            raise ToolError(str(exc)) from exc

    def _news_search(self, args: dict) -> str:
        try:
            return self.web.news(args["query"])
        except WebError as exc:
            raise ToolError(str(exc)) from exc

    def _read_webpage(self, args: dict) -> str:
        try:
            return self.web.read(args["url"].strip())
        except WebError as exc:
            raise ToolError(str(exc)) from exc

    def _trade_add(self, args: dict) -> str:
        trade = self.trades.add(
            args["item"], args["buy_price"], args.get("quantity", 1), args.get("target_price"), args.get("note", "")
        )
        return "Notiert: " + self.trades.describe(trade)

    def _trade_update(self, args: dict) -> str:
        if "current_price" not in args and "sold_price" not in args:
            raise ToolError("Gib current_price (aktueller Marktpreis) oder sold_price (verkauft für) an.")
        try:
            trade = self.trades.update(args["trade_id"], args.get("current_price"), args.get("sold_price"))
        except KeyError as exc:
            raise ToolError(f"Kein Investment mit der Nummer {args['trade_id']}.") from exc
        return self.trades.describe(trade)

    def _trade_overview(self, args: dict) -> str:
        return self.trades.overview(args.get("include_closed", False))

    def _find_skills(self, args: dict) -> str:
        if not self.skills.index():
            return f"Keine Skill-Bibliothek gefunden unter {self.skills.root}."
        hits = self.skills.find(args["query"])
        if not hits:
            return "Keine passenden Playbooks gefunden. Versuche andere englische Stichworte."
        return "\n".join(f"{h['path']} — {h['name']}: {h['description']}" for h in hits)

    def _read_skill_file(self, args: dict) -> str:
        try:
            return self.skills.read(args["path"])
        except (PermissionError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

    # -- registry -----------------------------------------------------------

    def _build(self) -> list[Tool]:
        text = {"type": "string", "minLength": 1}
        return [
            Tool(
                "get_weather",
                "Current weather and forecast for a place. Call this whenever the user asks about weather, "
                "temperature, rain, what to wear outside, or sunrise/sunset. Without a location the user's "
                "home location is used.",
                _obj({
                    "location": {"type": "string", "description": "City, optionally with country, e.g. 'München' or 'Paris, Frankreich'."},
                    "days": {"type": "integer", "minimum": 1, "maximum": 7, "description": "Forecast days: 1 = today, 2 = today and tomorrow, up to 7."},
                }),
                self._weather,
                slow=True,
            ),
            Tool(
                "add_task",
                "Add an item to the user's to-do list. Call this when the user mentions something they need "
                "to do, asks you to note a task, or agrees to a follow-up.",
                _obj({
                    "title": {**text, "description": "The task, short and clear."},
                    "due": {"type": "string", "description": "Due date as the user said it, e.g. 'Freitag' or '2026-10-20'."},
                }, ("title",)),
                self._add_task,
            ),
            Tool(
                "list_tasks",
                "Read the user's to-do list. Call this when the user asks what is open, on their list, or planned.",
                _obj({"include_done": {"type": "boolean", "description": "Also list finished tasks."}}),
                self._list_tasks,
            ),
            Tool(
                "complete_task",
                "Mark a to-do item as done, by the numeric id from list_tasks or add_task.",
                _obj({"task_id": {"type": "integer", "minimum": 1}}, ("task_id",)),
                self._complete_task,
            ),
            Tool(
                "save_note",
                "Save text as a Markdown note in the user's notes folder. Use it for ideas, meeting notes, "
                "drafts, plans and any answer too long to speak: save the full text, then give a short spoken "
                "summary. Saving under the title of an existing note appends to that note.",
                _obj({
                    "title": {**text, "description": "Short title, e.g. 'Business-Idee Kaffeeabo'."},
                    "content": {**text, "description": "Full note text; Markdown is fine here."},
                }, ("title", "content")),
                self._save_note,
            ),
            Tool(
                "search_notes",
                "Search the user's notes by keywords (all words must occur); an empty query lists the most "
                "recent notes. Call this when the user refers to something noted earlier.",
                _obj({"query": {"type": "string"}}),
                self._search_notes,
            ),
            Tool(
                "read_note",
                "Read a note in full, by the id from search_notes or by its exact title.",
                _obj({"note": text}, ("note",)),
                self._read_note,
            ),
            Tool(
                "remember",
                "Store a lasting fact about the user in long-term memory: name, preferred form of address "
                "(Sie/Du, title), preferences, important people, business details, goals. Call this whenever "
                "the user shares something worth knowing in future conversations. Write one complete German "
                "sentence per fact.",
                _obj({"fact": text}, ("fact",)),
                self._remember,
            ),
            Tool(
                "forget",
                "Delete a fact from long-term memory by its id (shown in brackets in your memory list). Call "
                "this when the user asks you to forget something or a stored fact is outdated.",
                _obj({"memory_id": {"type": "integer", "minimum": 1}}, ("memory_id",)),
                self._forget,
            ),
            Tool(
                "set_reminder",
                "Set a reminder or timer; you will announce it aloud when it is due (while Jarvis is running). "
                "Give exactly one of: in_minutes for relative times ('in zehn Minuten'), or at for absolute times "
                "as ISO 8601 with UTC offset, computed from the current time in the latest system note.",
                _obj({
                    "text": {**text, "description": "What to remind about, phrased as it should be spoken."},
                    "in_minutes": {"type": "number", "minimum": 0.1, "maximum": 525600},
                    "at": {"type": "string", "description": "e.g. '2026-10-14T08:30:00+02:00'"},
                }, ("text",)),
                self._set_reminder,
            ),
            Tool(
                "list_reminders",
                "List upcoming reminders with their ids and times.",
                _obj({}),
                self._list_reminders,
            ),
            Tool(
                "cancel_reminder",
                "Cancel an upcoming reminder by its id.",
                _obj({"reminder_id": {"type": "integer", "minimum": 1}}, ("reminder_id",)),
                self._cancel_reminder,
            ),
            Tool(
                "open_website",
                "Open a web page in the default browser on this computer. Call this when the user asks you to "
                "open, show or start a website or online service (e.g. YouTube search results, a map, a news site).",
                _obj({"url": {**text, "description": "Full URL starting with https://"}}, ("url",)),
                self._open_website,
            ),
            Tool(
                "web_search",
                "Search the web for current information: news, prices, opening hours, events and facts that "
                "may be newer than your knowledge. Returns titles, snippets and links; open the most promising "
                "result with read_webpage when the snippets are not enough.",
                _obj({
                    "query": {**text, "description": "Search words, e.g. 'Gründerzuschuss 2026 Voraussetzungen'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                }, ("query",)),
                self._web_search,
                slow=True,
                web=True,
            ),
            Tool(
                "news_search",
                "Find recent news articles about a topic. Call this when the user asks what is new or happening.",
                _obj({"query": text}, ("query",)),
                self._news_search,
                slow=True,
                web=True,
            ),
            Tool(
                "read_webpage",
                "Read the text of a web page, e.g. a search result, to answer from its content.",
                _obj({"url": {**text, "description": "Full URL starting with https://"}}, ("url",)),
                self._read_webpage,
                slow=True,
                web=True,
            ),
            Tool(
                "trade_add",
                "Record an EA FC Ultimate Team investment the user bought (player card or item, price in coins). "
                "Call this when the user says they bought something to trade or invest in. Profits are computed "
                "after the 5 % EA tax. You cannot see live market prices or trade in the game yourself.",
                _obj({
                    "item": {**text, "description": "Card or item, e.g. 'Mbappé TOTW' or 'Gold-Fitness'."},
                    "buy_price": {"type": "integer", "minimum": 1, "description": "Price paid per piece in coins."},
                    "quantity": {"type": "integer", "minimum": 1},
                    "target_price": {"type": "integer", "minimum": 1, "description": "Planned selling price per piece."},
                    "note": {"type": "string", "description": "Why bought, e.g. 'vor SBC am Freitag'."},
                }, ("item", "buy_price")),
                self._trade_add,
            ),
            Tool(
                "trade_update",
                "Update an investment by its id from trade_overview: the current market price the user reports, or "
                "the price it was sold for (closes the investment and books the profit after EA tax).",
                _obj({
                    "trade_id": {"type": "integer", "minimum": 1},
                    "current_price": {"type": "integer", "minimum": 1},
                    "sold_price": {"type": "integer", "minimum": 1},
                }, ("trade_id",)),
                self._trade_update,
            ),
            Tool(
                "trade_overview",
                "Show the user's EA FC investments with break-even, target and current profit after the 5 % EA tax, "
                "plus the realized profit so far. Call this when the user asks how their trades or investments are doing.",
                _obj({"include_closed": {"type": "boolean", "description": "Also list sold investments."}}),
                self._trade_overview,
            ),
            Tool(
                "find_skills",
                "Search the expert playbook library (about 350 guides on business strategy, founding, finance, "
                "pricing, marketing, sales, product, operations, compliance and engineering). Call this before "
                "answering substantial business or methodology questions, then read the best match with "
                "read_skill_file. Use English keywords, e.g. 'pricing strategy saas' or 'cash flow forecast'.",
                _obj({"query": text}, ("query",)),
                self._find_skills,
            ),
            Tool(
                "read_skill_file",
                "Read a playbook from the library: a SKILL.md path returned by find_skills, or a reference file it "
                "links to (path relative to the library root). Apply its framework to the user's situation instead "
                "of reading it aloud.",
                _obj({"path": text}, ("path",)),
                self._read_skill_file,
            ),
        ]
