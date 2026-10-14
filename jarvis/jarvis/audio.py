"""Microphone, speaker playback, voice-activity detection and the wake word.

``sounddevice`` (PortAudio) is imported lazily so text mode works on
machines without audio libraries.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np


class AudioUnavailable(RuntimeError):
    """No usable audio backend (PortAudio missing, no device, no permission)."""


def _sd():
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        raise AudioUnavailable(
            "Audio nicht verfügbar: sounddevice/PortAudio fehlt. "
            "pip install sounddevice (Linux zusätzlich: sudo apt install libportaudio2)."
        ) from exc
    return sounddevice


def _device(value: str):
    """Device setting: empty = system default, a number = index, else a name."""
    if not value:
        return None
    return int(value) if value.isdigit() else value


@dataclass
class AudioClip:
    samples: np.ndarray  # float32, mono, range -1..1
    rate: int

    @property
    def seconds(self) -> float:
        return len(self.samples) / self.rate if self.rate else 0.0


def rms(frame: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(frame)))) if len(frame) else 0.0


def chime(rate: int = 24000) -> AudioClip:
    """A short two-tone 'I'm listening' sound."""
    parts = []
    for freq in (880.0, 1320.0):
        t = np.arange(int(rate * 0.07)) / rate
        tone = 0.18 * np.sin(2 * np.pi * freq * t)
        fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)
        parts.append((tone * fade).astype(np.float32))
    return AudioClip(np.concatenate(parts), rate)


class Player:
    """Plays one clip at a time; ``cancelled`` is polled every 50 ms to interrupt."""

    BLOCK_SECONDS = 0.05

    def __init__(self, device: str = ""):
        self.device = _device(device)
        _sd()  # fail early with a clear message

    def play(self, clip: AudioClip, cancelled: Callable[[], bool] = lambda: False) -> None:
        sd = _sd()
        block = max(256, int(clip.rate * self.BLOCK_SECONDS))
        samples = np.ascontiguousarray(clip.samples, dtype=np.float32).reshape(-1, 1)
        with sd.OutputStream(samplerate=clip.rate, channels=1, dtype="float32", device=self.device) as stream:
            for start in range(0, len(samples), block):
                if cancelled():
                    break
                stream.write(samples[start:start + block])


class Microphone:
    """16 kHz mono input in 80 ms frames (the frame size openWakeWord expects)."""

    RATE = 16000
    FRAME = 1280
    FPS = RATE / FRAME

    def __init__(self, device: str = ""):
        self.device = _device(device)
        self.stream = None
        self.noise = 0.004  # running estimate of the room's background level

    def __enter__(self) -> "Microphone":
        sd = _sd()
        try:
            self.stream = sd.InputStream(
                samplerate=self.RATE, channels=1, dtype="float32", blocksize=self.FRAME, device=self.device
            )
            self.stream.start()
        except Exception as exc:
            raise AudioUnavailable(f"Mikrofon konnte nicht geöffnet werden: {exc}") from exc
        self.calibrate()
        return self

    def __exit__(self, *exc) -> None:
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def read(self) -> np.ndarray:
        data, _overflowed = self.stream.read(self.FRAME)
        return data[:, 0].copy()

    def drain(self) -> None:
        """Discard buffered audio (for example Jarvis' own voice)."""
        while self.stream.read_available >= self.FRAME:
            self.read()

    def calibrate(self, seconds: float = 0.5) -> None:
        levels = [rms(self.read()) for _ in range(max(1, int(seconds * self.FPS)))]
        self.noise = max(float(np.median(levels)), 0.001)

    def record_utterance(
        self, start_timeout: float | None = 8.0, max_seconds: float = 30.0, silence_seconds: float = 0.8
    ) -> np.ndarray | None:
        """Record one spoken request; ends after a pause. None if nobody spoke."""
        preroll: deque[np.ndarray] = deque(maxlen=4)
        frames: list[np.ndarray] = []
        loud = waited = silent = 0
        started = False
        while True:
            frame = self.read()
            level = rms(frame)
            threshold = max(self.noise * 3.5, 0.01)
            if not started:
                preroll.append(frame)
                if level > threshold:
                    loud += 1
                    if loud >= 2:
                        started = True
                        frames.extend(preroll)
                else:
                    loud = 0
                    waited += 1
                    self.noise = 0.95 * self.noise + 0.05 * level
                    if start_timeout is not None and waited / self.FPS > start_timeout:
                        return None
                continue
            frames.append(frame)
            silent = silent + 1 if level < threshold * 0.8 else 0
            if silent / self.FPS >= silence_seconds or len(frames) / self.FPS >= max_seconds:
                break
        audio = np.concatenate(frames)
        return audio if len(audio) >= self.RATE * 0.3 else None


class WakeWord:
    """'Hey Jarvis' detection with openWakeWord (runs locally, free)."""

    def __init__(self, threshold: float = 0.5):
        try:
            import openwakeword.utils
            from openwakeword.model import Model
        except ImportError as exc:
            raise AudioUnavailable(
                "Wake-Word braucht openwakeword: pip install -r requirements-wake.txt"
            ) from exc
        try:
            openwakeword.utils.download_models(["hey_jarvis"])  # one-time, a few MB
        except Exception:
            pass  # offline: use what is already downloaded
        self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
        self.threshold = threshold

    def detect(self, frame: np.ndarray) -> bool:
        pcm = (np.clip(frame, -1.0, 1.0) * 32767).astype(np.int16)
        scores = self.model.predict(pcm)
        return max(scores.values(), default=0.0) >= self.threshold

    def reset(self) -> None:
        self.model.reset()
