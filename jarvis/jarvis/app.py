"""The main loop: listen, think, speak."""

from __future__ import annotations

import random
from datetime import datetime

import anthropic
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.theme import Theme

from .audio import AudioUnavailable, Microphone, Player, WakeWord, chime
from .brain import Brain, Refusal
from .config import Settings
from .local_brain import LocalBrain, LocalModelError
from .speech import SentenceChunker, SilentSpeaker, Speaker
from .tools import Toolbox
from .tts import create_tts

THEME = Theme({"jarvis": "bold red", "user": "bold green", "hint": "dim", "warn": "yellow", "err": "bold red"})

TOOL_LABELS = {
    "get_weather": "Wetter", "add_task": "Aufgaben", "list_tasks": "Aufgaben", "complete_task": "Aufgaben",
    "save_note": "Notizen", "search_notes": "Notizen", "read_note": "Notizen",
    "remember": "Gedächtnis", "forget": "Gedächtnis",
    "set_reminder": "Erinnerungen", "list_reminders": "Erinnerungen", "cancel_reminder": "Erinnerungen",
    "open_website": "Browser", "trade_add": "Trading", "trade_update": "Trading", "trade_overview": "Trading", "find_skills": "Playbooks", "read_skill_file": "Playbooks",
    "web_search": "Websuche",
}
FILLERS = ["Einen Moment.", "Ich sehe nach.", "Sofort.", "Einen Augenblick."]
HELP = (
    "/neu – neues Gespräch   /kosten – Verbrauch dieser Sitzung   "
    "/gedaechtnis – was Jarvis über dich weiß   /ende – beenden"
)


def greeting(now: datetime) -> str:
    if 5 <= now.hour < 11:
        return "Guten Morgen. Alle Systeme sind bereit."
    if 11 <= now.hour < 18:
        return "Guten Tag. Alle Systeme sind bereit."
    if 18 <= now.hour < 23:
        return "Guten Abend. Alle Systeme sind bereit."
    return "Noch wach? Alle Systeme sind bereit."


class Jarvis:
    def __init__(self, settings: Settings, mute: bool = False, console: Console | None = None):
        self.settings = settings
        self.console = console or Console(theme=THEME)
        self.toolbox = Toolbox(settings)
        self.brain = LocalBrain(settings, self.toolbox) if settings.is_local else Brain(settings, self.toolbox)
        tts = None if mute else create_tts(settings)
        if tts is None:
            self.speaker = SilentSpeaker()
        else:
            self.speaker = Speaker(tts, Player(settings.output_device), on_error=self._warn)
        self.toolbox.reminders.start(self._on_reminder)

    # -- output helpers -----------------------------------------------------

    def _warn(self, message: str) -> None:
        self.console.print("\n" + message, style="warn", markup=False)

    def _on_reminder(self, item: dict) -> None:
        text = f"Erinnerung: {item['text']}"
        self.console.print(f"\n⏰ {text}", style="jarvis", markup=False)
        self.speaker.say(text)

    def _say(self, text: str) -> None:
        self.console.print(f"[jarvis]Jarvis:[/] {escape(text)}")
        self.speaker.say(text)

    def banner(self, mode: str) -> None:
        voice = {"edge": self.settings.edge_voice, "elevenlabs": "ElevenLabs", "piper": "Piper (offline)"}.get(
            self.settings.tts, "aus"
        )
        if isinstance(self.speaker, SilentSpeaker):
            voice = "aus"
        self.console.print(Panel.fit(
            f"[jarvis]J.A.R.V.I.S.[/]\n[hint]Gehirn {escape(self.brain.label)} · Stimme {voice} · Modus {mode}[/]",
            border_style="red",
        ))

    def finish_speaking(self) -> None:
        try:
            self.speaker.wait()
        except KeyboardInterrupt:
            self.speaker.stop()

    # -- one exchange -------------------------------------------------------

    def ask(self, text: str) -> None:
        """Send one message to Claude and speak the answer while it streams in."""
        chunker = SentenceChunker()
        state = {"spoken": False, "filler": False}
        self.console.print("[jarvis]Jarvis:[/] ", end="")

        def on_text(delta: str) -> None:
            self.console.print(delta, end="", style="red", markup=False, highlight=False)
            for sentence in chunker.feed(delta):
                state["spoken"] = True
                self.speaker.say(sentence)

        def on_tool(name: str, slow: bool) -> None:
            self.console.print(f"[hint]⚙ {TOOL_LABELS.get(name, name)}[/] ", end="")
            if slow and not state["spoken"] and not state["filler"]:
                state["filler"] = True
                self.speaker.say(random.choice(FILLERS))

        try:
            self.brain.respond(text, on_text, on_tool)
            for sentence in chunker.flush():
                self.speaker.say(sentence)
            self.console.print()
        except KeyboardInterrupt:
            self.speaker.stop()
            self.console.print("\n[hint](unterbrochen)[/]")
        except Refusal:
            self.speaker.stop()
            self.console.print()
            self._say("Dabei kann ich leider nicht helfen.")
        except Exception as exc:  # API and network problems: explain, keep running
            self.speaker.stop()
            self.console.print()
            self._report_error(exc)

    def _report_error(self, exc: Exception) -> None:
        if isinstance(exc, LocalModelError):
            detail, spoken = str(exc), "Mein lokales Sprachmodell ist nicht bereit. Details stehen im Terminal."
        elif isinstance(exc, TypeError) and "authentication" in str(exc):
            detail, spoken = "Kein API-Schlüssel gefunden: ANTHROPIC_API_KEY in jarvis/.env eintragen.", "Mir fehlt der API-Schlüssel."
        elif isinstance(exc, anthropic.AuthenticationError):
            detail, spoken = "Der API-Schlüssel wurde abgelehnt. Bitte ANTHROPIC_API_KEY prüfen.", "Mein API-Schlüssel wurde abgelehnt."
        elif isinstance(exc, anthropic.NotFoundError):
            detail, spoken = f"Modell nicht verfügbar: {self.settings.model}", "Das eingestellte Modell ist nicht verfügbar."
        elif isinstance(exc, anthropic.RateLimitError):
            detail, spoken = f"Limit erreicht: {exc.message}", "Ich habe gerade mein Nutzungslimit erreicht."
        elif isinstance(exc, anthropic.BadRequestError):
            detail, spoken = f"Anfrage abgelehnt: {exc.message}", "Die Anfrage wurde abgelehnt. Details stehen im Terminal."
        elif isinstance(exc, anthropic.APIStatusError):
            detail, spoken = f"Claude-Fehler {exc.status_code}: {exc.message}", "Claude ist gerade nicht erreichbar. Versuchen Sie es gleich noch einmal."
        elif isinstance(exc, anthropic.APIConnectionError):
            detail, spoken = "Keine Verbindung zu Claude. Ist das Internet verfügbar?", "Ich erreiche Claude gerade nicht."
        else:
            detail, spoken = f"{type(exc).__name__}: {exc}", "Da ist etwas schiefgelaufen. Details stehen im Terminal."
        self.console.print(detail, style="err", markup=False)
        self.speaker.say(spoken)

    # -- slash commands (text mode) -----------------------------------------

    def command(self, line: str) -> bool:
        """Handle a /command; returns False when Jarvis should exit."""
        cmd = line.strip().lower().split()[0]
        if cmd in ("/ende", "/exit", "/quit"):
            return False
        if cmd == "/neu":
            self.brain.new_conversation()
            self.console.print("[hint]Neues Gespräch begonnen.[/]")
        elif cmd == "/kosten":
            u = self.brain.usage
            if self.settings.is_local:
                self.console.print(f"[hint]Lokal und kostenlos: {u.input} Tokens gelesen, {u.output} geschrieben.[/]")
            else:
                cost = u.cost(self.settings.model)
                cost_txt = f" ≈ {cost:.3f} USD" if cost is not None else ""
                self.console.print(
                    f"[hint]Tokens: {u.input} Eingabe, {u.cache_read} aus Cache, {u.cache_write} in Cache, "
                    f"{u.output} Ausgabe · {u.searches} Websuchen{cost_txt}[/]"
                )
        elif cmd in ("/gedaechtnis", "/gedächtnis"):
            facts = self.toolbox.memory.all()
            if not facts:
                self.console.print("[hint]Noch nichts gespeichert.[/]")
            for fact in facts:
                self.console.print(f"[{fact['id']}] {fact['fact']}", style="hint", markup=False)
        else:
            self.console.print(f"[hint]{HELP}[/]")
        return True

    # -- modes --------------------------------------------------------------

    def run_text(self) -> None:
        self.banner("Text")
        self.console.print(f"[hint]{HELP}[/]")
        self._say(greeting(self.settings.now()))
        while True:
            try:
                line = self.console.input("[user]Du:[/] ").strip()
            except KeyboardInterrupt:
                if self.speaker.busy:  # Ctrl+C while Jarvis is still talking: just stop the voice
                    self.speaker.stop()
                    self.console.print()
                    continue
                break
            except EOFError:
                break
            if not line:
                continue
            if line.startswith("/"):
                if not self.command(line):
                    break
                continue
            self.speaker.stop()  # a new message interrupts the previous answer
            self.ask(line)
        self.console.print("\n[hint]Bis bald.[/]")

    def _load_stt(self):
        from .stt import create_stt

        with self.console.status("Lade Spracherkennung (beim ersten Start wird das Modell heruntergeladen) …"):
            try:
                return create_stt(self.settings)
            except AudioUnavailable:
                raise
            except Exception as exc:  # e.g. model download failed
                raise AudioUnavailable(f"Spracherkennung konnte nicht geladen werden: {exc}") from exc

    def _listen(self, mic: Microphone, stt, start_timeout: float | None) -> str:
        audio = mic.record_utterance(start_timeout=start_timeout)
        if audio is None:
            return ""
        with self.console.status("Verstehe …"):
            text = stt.transcribe(audio)
        if text:
            self.console.print(f"[user]Du:[/] {escape(text)}", highlight=False)
        return text

    def run_push_to_talk(self) -> None:
        stt = self._load_stt()
        self.banner("Push-to-Talk")
        self._say(greeting(self.settings.now()))
        with Microphone(self.settings.input_device) as mic:
            while True:
                self.finish_speaking()
                try:
                    self.console.input("[hint]⏎ Enter drücken und sprechen · Strg+C beendet[/] ")
                except (EOFError, KeyboardInterrupt):
                    break
                self.speaker.stop()
                self.speaker.play(chime())
                self.speaker.wait()
                mic.drain()
                self.console.print("[hint]🎙  Ich höre …[/]")
                try:
                    text = self._listen(mic, stt, start_timeout=8.0)
                except KeyboardInterrupt:
                    continue
                if text:
                    self.ask(text)
                else:
                    self.console.print("[hint](nichts verstanden)[/]")
        self.console.print("\n[hint]Bis bald.[/]")

    def run_wake_word(self) -> None:
        stt = self._load_stt()
        with self.console.status("Lade Wake-Word-Erkennung …"):
            wake = WakeWord(self.settings.wake_threshold)
        self.banner("Hey Jarvis")
        self._say(greeting(self.settings.now()))
        self.finish_speaking()
        with Microphone(self.settings.input_device) as mic:
            self.console.print('[hint]Sag „Hey Jarvis“ … (Strg+C beendet)[/]')
            try:
                while True:
                    frame = mic.read()
                    if self.speaker.busy:  # never react to our own voice
                        wake.reset()
                        continue
                    if not wake.detect(frame):
                        continue
                    wake.reset()
                    self._conversation(mic, stt)
                    mic.drain()
                    self.console.print('[hint]Sag „Hey Jarvis“ …[/]')
            except KeyboardInterrupt:
                pass
        self.console.print("\n[hint]Bis bald.[/]")

    def _conversation(self, mic: Microphone, stt) -> None:
        """After the wake word: take the request, then allow follow-ups without it."""
        self.speaker.play(chime())
        # The request often follows "Hey Jarvis" in one breath, so keep the
        # buffered audio; a second attempt covers a recording of just the chime.
        text = self._listen(mic, stt, start_timeout=5.0) or self._listen(mic, stt, start_timeout=4.0)
        while text:
            self.ask(text)
            self.finish_speaking()
            mic.drain()
            self.console.print("[hint](ich höre weiter zu …)[/]")
            text = self._listen(mic, stt, start_timeout=6.0)
