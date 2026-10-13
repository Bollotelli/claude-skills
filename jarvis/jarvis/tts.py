"""Text-to-speech backends: Jarvis' voice.

- edge:       Microsoft neural voices via edge-tts. Free, no key, very natural
              German voices. Unofficial interface, needs internet.
- elevenlabs: Studio-quality, most expressive voices; needs an API key.
- piper:      Runs fully offline on your computer; good quality, no costs.
"""

from __future__ import annotations

import asyncio
import io
import os
import wave

import numpy as np

from .audio import AudioClip
from .config import Settings


class TTSError(RuntimeError):
    pass


def decode_audio(data: bytes, rate: int = 24000) -> AudioClip:
    """Decode MP3/WAV/FLAC/OGG bytes to a mono float32 clip."""
    import miniaudio

    decoded = miniaudio.decode(data, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=rate)
    return AudioClip(np.frombuffer(decoded.samples, dtype=np.float32).copy(), decoded.sample_rate)


def wav_bytes(clip: AudioClip) -> bytes:
    pcm = (np.clip(clip.samples, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(clip.rate)
        wav.writeframes(pcm.tobytes())
    return buf.getvalue()


class EdgeTTS:
    name = "edge"
    FALLBACK_VOICE = "de-DE-ConradNeural"

    def __init__(self, voice: str, rate: str = "+0%", pitch: str = "+0Hz"):
        import edge_tts  # noqa: F401  (fail early if missing)

        self.voice, self.rate, self.pitch = voice, rate, pitch
        self.proxy = os.environ.get("HTTPS_PROXY") or None

    def synthesize(self, text: str) -> AudioClip:
        from edge_tts.exceptions import NoAudioReceived

        try:
            return asyncio.run(self._synthesize(text, self.voice))
        except (NoAudioReceived, TTSError):  # unknown voice name; network errors propagate
            if self.voice == self.FALLBACK_VOICE:
                raise
            clip = asyncio.run(self._synthesize(text, self.FALLBACK_VOICE))
            print(f"[Stimme] '{self.voice}' ist nicht verfügbar, ich nutze '{self.FALLBACK_VOICE}'.")
            self.voice = self.FALLBACK_VOICE
            return clip

    async def _synthesize(self, text: str, voice: str) -> AudioClip:
        import edge_tts

        communicate = edge_tts.Communicate(text, voice, rate=self.rate, pitch=self.pitch, proxy=self.proxy)
        mp3 = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3.extend(chunk["data"])
        if not mp3:
            raise TTSError("edge-tts lieferte kein Audio (Stimme gültig? Internet?)")
        return decode_audio(bytes(mp3))

    @staticmethod
    def list_voices(prefixes: tuple[str, ...] = ("de-", "en-GB-")) -> list[dict]:
        import edge_tts

        voices = asyncio.run(edge_tts.list_voices(proxy=os.environ.get("HTTPS_PROXY") or None))
        return [v for v in voices if v["ShortName"].startswith(prefixes)]


class ElevenLabsTTS:
    name = "elevenlabs"
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    def __init__(self, api_key: str, voice_id: str, model: str, language: str = "de"):
        if not api_key:
            raise TTSError("ELEVENLABS_API_KEY fehlt in der .env-Datei.")
        self.api_key, self.voice_id, self.model, self.language = api_key, voice_id, model, language

    def synthesize(self, text: str) -> AudioClip:
        import httpx2 as httpx  # ships with the anthropic SDK

        body = {"text": text, "model_id": self.model}
        if "v2_5" in self.model:  # only the v2.5 models accept a language override
            body["language_code"] = self.language
        response = httpx.post(
            self.URL.format(voice_id=self.voice_id),
            params={"output_format": "pcm_24000"},
            headers={"xi-api-key": self.api_key},
            json=body,
            timeout=30.0,
        )
        if response.status_code != 200:
            raise TTSError(f"ElevenLabs-Fehler {response.status_code}: {response.text[:200]}")
        samples = np.frombuffer(response.content, dtype="<i2").astype(np.float32) / 32768.0
        return AudioClip(samples, 24000)


class PiperTTS:
    name = "piper"

    def __init__(self, model_path: str):
        if not model_path or not os.path.exists(os.path.expanduser(model_path)):
            raise TTSError(
                "Piper-Stimme nicht gefunden. Lade eine herunter, z. B.:\n"
                "  python -m piper.download_voices --download-dir ~/.jarvis/stimmen de_DE-thorsten-high\n"
                "und setze JARVIS_PIPER_MODEL=~/.jarvis/stimmen/de_DE-thorsten-high.onnx"
            )
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise TTSError("Piper fehlt: pip install piper-tts") from exc
        self.voice = PiperVoice.load(os.path.expanduser(model_path))

    def synthesize(self, text: str) -> AudioClip:
        chunks = list(self.voice.synthesize(text))
        if not chunks:
            return AudioClip(np.zeros(0, dtype=np.float32), 22050)
        samples = np.concatenate([c.audio_float_array for c in chunks]).astype(np.float32)
        return AudioClip(samples, chunks[0].sample_rate)


def create_tts(settings: Settings):
    """The configured voice, or None for text-only output."""
    if settings.tts in ("none", "aus", "off"):
        return None
    if settings.tts == "elevenlabs":
        return ElevenLabsTTS(
            settings.elevenlabs_api_key, settings.elevenlabs_voice_id, settings.elevenlabs_model, settings.language
        )
    if settings.tts == "piper":
        return PiperTTS(settings.piper_model)
    try:
        return EdgeTTS(settings.edge_voice, settings.edge_rate, settings.edge_pitch)
    except ImportError as exc:
        raise TTSError("edge-tts fehlt: pip install -r requirements.txt") from exc
