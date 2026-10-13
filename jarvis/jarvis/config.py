"""Settings, read from environment variables and an optional ``.env`` file.

Every option has a sensible default, so Jarvis runs with nothing but an
``ANTHROPIC_API_KEY``. See ``.env.example`` for the documented list.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent  # jarvis/jarvis
APP_DIR = PACKAGE_DIR.parent  # jarvis/
REPO_ROOT = APP_DIR.parent  # the skills library this app lives in


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "ja", "on"}


def load_env_file() -> None:
    """Load ``jarvis/.env`` (and ``./.env``) without overriding real env vars."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(APP_DIR / ".env")
    load_dotenv(Path.cwd() / ".env")
    # An empty placeholder from .env.example would shadow an `ant auth login` profile.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if name in os.environ and not os.environ[name].strip():
            del os.environ[name]


@dataclass
class Settings:
    # Brain (Claude)
    model: str = "claude-opus-5-5"
    effort: str = "low"  # low keeps spoken replies snappy; medium/high think longer
    max_tokens: int = 64000
    web_search: bool = True
    idle_reset_minutes: int = 30

    # Who and where
    language: str = "de"
    location: str = "Berlin"
    country: str = "DE"
    timezone: str = "Europe/Berlin"

    # Voice output
    tts: str = "edge"  # edge | elevenlabs | piper | none
    edge_voice: str = "de-DE-FlorianMultilingualNeural"
    edge_rate: str = "+0%"
    edge_pitch: str = "+0Hz"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "JBFqnCBsd6RMkjVDRZzb"  # "George": warm, calm, British
    elevenlabs_model: str = "eleven_multilingual_v2"
    piper_model: str = ""

    # Voice input
    stt: str = "local"  # local (faster-whisper) | openai
    whisper_model: str = "small"
    openai_api_key: str = ""
    openai_stt_model: str = "whisper-1"
    wake_threshold: float = 0.5
    input_device: str = ""
    output_device: str = ""

    # Storage
    data_dir: Path = Path.home() / ".jarvis"
    skills_dir: Path = REPO_ROOT

    @classmethod
    def from_env(cls) -> "Settings":
        load_env_file()
        d = cls()
        return cls(
            model=_env("JARVIS_MODEL", d.model),
            effort=_env("JARVIS_EFFORT", d.effort),
            max_tokens=_env_int("JARVIS_MAX_TOKENS", d.max_tokens),
            web_search=_env_bool("JARVIS_WEB_SEARCH", d.web_search),
            idle_reset_minutes=_env_int("JARVIS_IDLE_RESET_MINUTES", d.idle_reset_minutes),
            language=_env("JARVIS_LANGUAGE", d.language),
            location=_env("JARVIS_LOCATION", d.location),
            country=_env("JARVIS_COUNTRY", d.country),
            timezone=_env("JARVIS_TIMEZONE", d.timezone),
            tts=_env("JARVIS_TTS", d.tts).lower(),
            edge_voice=_env("JARVIS_EDGE_VOICE", d.edge_voice),
            edge_rate=_env("JARVIS_EDGE_RATE", d.edge_rate),
            edge_pitch=_env("JARVIS_EDGE_PITCH", d.edge_pitch),
            elevenlabs_api_key=_env("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_env("JARVIS_ELEVENLABS_VOICE_ID", d.elevenlabs_voice_id),
            elevenlabs_model=_env("JARVIS_ELEVENLABS_MODEL", d.elevenlabs_model),
            piper_model=_env("JARVIS_PIPER_MODEL"),
            stt=_env("JARVIS_STT", d.stt).lower(),
            whisper_model=_env("JARVIS_WHISPER_MODEL", d.whisper_model),
            openai_api_key=_env("OPENAI_API_KEY"),
            openai_stt_model=_env("JARVIS_OPENAI_STT_MODEL", d.openai_stt_model),
            wake_threshold=_env_float("JARVIS_WAKE_THRESHOLD", d.wake_threshold),
            input_device=_env("JARVIS_INPUT_DEVICE"),
            output_device=_env("JARVIS_OUTPUT_DEVICE"),
            data_dir=Path(_env("JARVIS_DATA_DIR", str(d.data_dir))).expanduser(),
            skills_dir=Path(_env("JARVIS_SKILLS_DIR", str(d.skills_dir))).expanduser(),
        )

    def tz(self) -> tzinfo:
        """Configured time zone, falling back to the computer's local zone."""
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(self.timezone)
        except Exception:
            return datetime.now().astimezone().tzinfo

    def now(self) -> datetime:
        return datetime.now(self.tz())
