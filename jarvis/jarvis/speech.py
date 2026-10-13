"""From streamed text to spoken audio.

``SentenceChunker`` cuts Claude's token stream into sentences, ``Speaker``
synthesizes them in one thread and plays them in another, so the first
sentence is audible while the next ones are still being written.
"""

from __future__ import annotations

import queue
import re
import threading

ABBREVIATIONS = {
    "bzw", "ca", "dr", "prof", "nr", "str", "usw", "vgl", "evtl", "ggf", "inkl", "zzgl", "max", "min",
    "mio", "mrd", "tel", "etc", "co", "mr", "mrs", "vs", "st", "abs", "bspw", "geb", "gegr", "dt", "jh",
}
_BOUNDARY = re.compile(r"[.!?…]+[\"'»«“”)\]]*\s+|\n+")
_URL = re.compile(r"https?://\S+")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.MULTILINE)


def clean_for_speech(text: str) -> str:
    """Strip what should not be read aloud: Markdown, links, emoji."""
    text = _URL.sub("", text)
    text = _EMOJI.sub("", text)
    text = _LIST_MARKER.sub("", text)
    text = re.sub(r"[*`#]+|__+", "", text)
    return re.sub(r"\s+", " ", text).strip()


class SentenceChunker:
    """Collects streamed text deltas and emits speakable sentences."""

    def __init__(self, min_chars: int = 24):
        self.buffer = ""
        self.min_chars = min_chars  # merge very short sentences for smoother speech

    def feed(self, delta: str) -> list[str]:
        self.buffer += delta
        sentences = []
        while (cut := self._boundary()) is not None:
            sentence, self.buffer = self.buffer[:cut].strip(), self.buffer[cut:]
            if sentence:
                sentences.append(sentence)
        return sentences

    def flush(self) -> list[str]:
        rest, self.buffer = self.buffer.strip(), ""
        return [rest] if rest else []

    def _boundary(self) -> int | None:
        for match in _BOUNDARY.finditer(self.buffer):
            is_newline = match.group().startswith("\n")
            if not is_newline and match.end() < self.min_chars:
                continue
            if match.group().startswith(".") and self._is_abbreviation(match.start()):
                continue
            return match.end()
        return None

    def _is_abbreviation(self, dot: int) -> bool:
        word = re.search(r"(\S+)$", self.buffer[:dot])
        if not word:
            return False
        token = word.group(1).lower().strip("(\"'")
        last = token.split(".")[-1]
        # ordinals and dates ("13. Oktober"), initials and "z. B.", known abbreviations
        return token.isdigit() or len(last) <= 1 or last in ABBREVIATIONS


class Speaker:
    """Background speech output with interruption support."""

    def __init__(self, tts, player, on_error=print):
        self.tts, self.player, self.on_error = tts, player, on_error
        self._texts: queue.Queue = queue.Queue()
        self._audio: queue.Queue = queue.Queue(maxsize=3)
        self._cond = threading.Condition()
        self._pending = 0
        self._generation = 0
        self._error_shown = False
        threading.Thread(target=self._synth_loop, name="jarvis-tts", daemon=True).start()
        threading.Thread(target=self._play_loop, name="jarvis-play", daemon=True).start()

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if not text:
            return
        with self._cond:
            self._pending += 1
            generation = self._generation
        self._texts.put((generation, text))

    def play(self, clip) -> None:
        """Queue a ready-made clip (e.g. the listening chime)."""
        with self._cond:
            self._pending += 1
            generation = self._generation
        self._audio.put((generation, clip))

    def wait(self, timeout: float | None = None) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._pending == 0, timeout)

    def stop(self) -> None:
        """Silence immediately and drop everything queued."""
        with self._cond:
            self._generation += 1
        for q in (self._texts, self._audio):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
                self._done()

    def _done(self) -> None:
        with self._cond:
            self._pending -= 1
            self._cond.notify_all()

    def _synth_loop(self) -> None:
        while True:
            generation, text = self._texts.get()
            if generation != self._generation:
                self._done()
                continue
            try:
                clip = self.tts.synthesize(text)
            except Exception as exc:
                if not self._error_shown:
                    self.on_error(f"Sprachausgabe fehlgeschlagen ({exc}). Internet prüfen oder 'python -m jarvis sag' testen.")
                    self._error_shown = True
                self._done()
                continue
            self._error_shown = False
            self._audio.put((generation, clip))

    def _play_loop(self) -> None:
        while True:
            generation, clip = self._audio.get()
            try:
                if generation == self._generation:
                    self.player.play(clip, lambda: generation != self._generation)
            except Exception as exc:
                self.on_error(f"Wiedergabe fehlgeschlagen: {exc}")
            finally:
                self._done()


class SilentSpeaker:
    """Same interface as Speaker, for text-only mode."""

    busy = False

    def say(self, text: str) -> None:
        pass

    def play(self, clip) -> None:
        pass

    def wait(self, timeout: float | None = None) -> None:
        pass

    def stop(self) -> None:
        pass
