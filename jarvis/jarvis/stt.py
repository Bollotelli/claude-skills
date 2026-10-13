"""Speech-to-text: what you say becomes text for Claude.

- local:  faster-whisper on your own computer (free, private). The model is
          downloaded once on first use (~460 MB for "small").
- openai: OpenAI's transcription API (fast on weak hardware, paid, needs a key).
"""

from __future__ import annotations

import re

import numpy as np

from .audio import AudioClip, AudioUnavailable
from .config import Settings
from .tts import wav_bytes

# Lines Whisper is known to "hear" in silence or noise (learned from subtitles).
_HALLUCINATIONS = re.compile(
    r"^(untertitel(ung)?\b.*|vielen dank f(ü|ue)r(s|'s)? zuschauen.*|thanks for watching.*|.*amara\.org.*)$",
    re.IGNORECASE,
)


def clean_transcript(text: str) -> str:
    text = text.strip()
    if len(text) < 100 and _HALLUCINATIONS.match(text.strip(" .!")):
        return ""
    return text


class LocalWhisper:
    def __init__(self, model_size: str, language: str):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AudioUnavailable("Spracherkennung fehlt: pip install -r requirements.txt") from exc
        self.model = WhisperModel(model_size, device="auto", compute_type="int8")
        self.language = language

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _info = self.model.transcribe(
            audio, language=self.language, beam_size=1, vad_filter=True, condition_on_previous_text=False
        )
        return clean_transcript(" ".join(segment.text.strip() for segment in segments))


class OpenAITranscriber:
    URL = "https://api.openai.com/v1/audio/transcriptions"

    def __init__(self, api_key: str, model: str, language: str):
        if not api_key:
            raise AudioUnavailable("OPENAI_API_KEY fehlt in der .env-Datei.")
        self.api_key, self.model, self.language = api_key, model, language

    def transcribe(self, audio: np.ndarray) -> str:
        import httpx2 as httpx  # ships with the anthropic SDK

        response = httpx.post(
            self.URL,
            headers={"Authorization": f"Bearer {self.api_key}"},
            files={"file": ("sprache.wav", wav_bytes(AudioClip(audio, 16000)), "audio/wav")},
            data={"model": self.model, "language": self.language},
            timeout=60.0,
        )
        response.raise_for_status()
        return clean_transcript(response.json().get("text", ""))


def create_stt(settings: Settings):
    if settings.stt == "openai":
        return OpenAITranscriber(settings.openai_api_key, settings.openai_stt_model, settings.language)
    return LocalWhisper(settings.whisper_model, settings.language)
