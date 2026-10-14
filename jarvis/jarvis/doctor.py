"""``python -m jarvis check``: is everything set up? Explains how to fix what isn't."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .config import APP_DIR, Settings
from .local_brain import _is_local_host, normalize_host

OK, FAIL, OPTIONAL = "[green]✓[/]", "[red]✗[/]", "[yellow]–[/]"


def _module(name: str):
    try:
        return importlib.import_module(name)
    except Exception:
        return None


def check_local_brain(settings: Settings, add) -> None:
    ollama = _module("ollama")
    if ollama is None:
        add(FAIL, "Gehirn (Ollama)", "Paket fehlt – pip install -r requirements.txt")
        return
    try:
        extra = {"trust_env": False} if _is_local_host(settings.ollama_host) else {}
        client = ollama.Client(host=normalize_host(settings.ollama_host) or None, **extra)
        names = [m.model or "" for m in client.list().models]
    except Exception:
        add(FAIL, "Gehirn (Ollama)", "Ollama läuft nicht – installieren: https://ollama.com/download, dann die App starten")
        return
    wanted = settings.ollama_model
    if wanted not in names and f"{wanted}:latest" not in names:
        add(FAIL, "Gehirn (lokal, kostenlos)", f"Modell fehlt – einmalig: ollama pull {wanted}")
    else:
        try:
            capabilities = client.show(wanted).capabilities
        except Exception:
            capabilities = None  # older Ollama: no capability list, assume fine
        if capabilities is not None and "tools" not in capabilities:
            add(FAIL, "Gehirn (lokal, kostenlos)", f"{wanted} kann keine Werkzeuge nutzen – nimm z. B. qwen3:4b oder qwen3:8b")
        else:
            add(OK, "Gehirn (lokal, kostenlos)", f"{wanted} bereit")
    add(OPTIONAL, "API-Schlüssel", "nicht nötig: das lokale Gehirn ist kostenlos")


def check_claude(settings: Settings, online: bool, add) -> None:
    anthropic = _module("anthropic")
    version = getattr(anthropic, "__version__", "")
    major = int(version.split(".")[0]) if version[:1].isdigit() else 0
    add(OK if major >= 1 else FAIL, "Claude-SDK (anthropic)", version or "fehlt – pip install -r requirements.txt")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    profile = Path.home() / ".config" / "anthropic"
    if key:
        add(OK, "API-Schlüssel", f"gesetzt ({key[:7]}…)")
    elif profile.exists():
        add(OK, "API-Schlüssel", "kein ANTHROPIC_API_KEY, aber ein 'ant auth login'-Profil gefunden")
    else:
        add(FAIL, "API-Schlüssel", f"ANTHROPIC_API_KEY in {APP_DIR / '.env'} eintragen (console.anthropic.com)")

    if online and anthropic and (key or profile.exists()):
        try:
            model = anthropic.Anthropic().models.retrieve(settings.model)
            add(OK, "Verbindung zu Claude", f"{model.id} erreichbar")
        except Exception as exc:
            add(FAIL, "Verbindung zu Claude", f"{type(exc).__name__}: {str(exc)[:120]}")


def run_checks(settings: Settings, online: bool = False) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []

    def add(status: str, label: str, detail: str = "") -> None:
        rows.append((status, label, detail))

    py = sys.version_info
    add(OK if py >= (3, 10) else FAIL, "Python", f"{py.major}.{py.minor}" + ("" if py >= (3, 10) else " – bitte 3.10 bis 3.12 installieren"))

    if settings.is_local:
        check_local_brain(settings, add)
        add(OK if _module("ddgs") else FAIL, "Websuche (kostenlos)", "DuckDuckGo & Co." if _module("ddgs") else "pip install ddgs")
    else:
        check_claude(settings, online, add)

    tts_modules = {"edge": "edge_tts", "elevenlabs": "httpx2", "piper": "piper"}
    if settings.tts in ("none", "aus", "off"):
        add(OPTIONAL, "Stimme", "ausgeschaltet (JARVIS_TTS=none)")
    else:
        found = _module(tts_modules.get(settings.tts, "edge_tts"))
        detail = {"edge": settings.edge_voice, "elevenlabs": f"Stimme {settings.elevenlabs_voice_id}", "piper": settings.piper_model}.get(settings.tts, settings.tts)
        problem = ""
        if not found:
            problem = " – Paket fehlt (pip install -r requirements.txt)"
        elif settings.tts == "elevenlabs" and not settings.elevenlabs_api_key:
            problem = " – ELEVENLABS_API_KEY fehlt"
        elif settings.tts == "piper" and not (settings.piper_model and Path(settings.piper_model).expanduser().is_file()):
            problem = " – Stimmdatei nicht gefunden (siehe README, Abschnitt Piper)"
        add(FAIL if problem else OK, f"Stimme ({settings.tts})", detail + problem)
    add(OK if _module("miniaudio") else FAIL, "MP3-Decoder (miniaudio)", "" if _module("miniaudio") else "pip install miniaudio")

    sd = _module("sounddevice")
    if sd is None:
        add(FAIL, "Audio (sounddevice)", "fehlt oder PortAudio fehlt – Linux: sudo apt install libportaudio2")
    else:
        for kind, label in (("input", "Mikrofon"), ("output", "Lautsprecher")):
            try:
                device = sd.query_devices(kind=kind)
                add(OK, label, device["name"])
            except Exception as exc:
                add(FAIL, label, f"kein Gerät gefunden ({exc})")

    if settings.stt == "openai":
        add(OK if settings.openai_api_key else FAIL, "Spracherkennung (OpenAI)", "" if settings.openai_api_key else "OPENAI_API_KEY fehlt")
    else:
        found = _module("faster_whisper")
        add(OK if found else FAIL, "Spracherkennung (lokal)", f"Whisper-Modell '{settings.whisper_model}'" if found else "pip install -r requirements.txt")

    add(OK if _module("openwakeword") else OPTIONAL, "„Hey Jarvis“ (openwakeword)", "" if _module("openwakeword") else "optional: pip install -r requirements-wake.txt")

    try:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        probe = settings.data_dir / ".schreibtest"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        add(OK, "Datenordner", str(settings.data_dir))
    except OSError as exc:
        add(FAIL, "Datenordner", f"{settings.data_dir}: {exc}")

    paid = []
    if not settings.is_local:
        paid.append("Claude-API")
    if settings.tts == "elevenlabs":
        paid.append("ElevenLabs-Stimme")
    if settings.stt == "openai":
        paid.append("OpenAI-Spracherkennung")
    if paid:
        add(OPTIONAL, "Kosten", "kostenpflichtig: " + ", ".join(paid))
    else:
        add(OK, "Kosten", "komplett kostenlos")

    from .tools.skills import SkillLibrary

    count = len(SkillLibrary(settings.skills_dir).index())
    add(OK if count else OPTIONAL, "Skill-Bibliothek", f"{count} Playbooks in {settings.skills_dir}" if count else "nicht gefunden (Business-Playbooks deaktiviert)")
    return rows


def main(settings: Settings, online: bool = False) -> int:
    console = Console()
    table = Table(title="J.A.R.V.I.S. – Einrichtung", show_header=False, box=None, padding=(0, 1))
    rows = run_checks(settings, online)
    for status, label, detail in rows:
        table.add_row(status, label, f"[dim]{detail}[/]")
    console.print(table)
    failed = [label for status, label, _ in rows if status == FAIL]
    if failed:
        console.print(f"\n[red]Noch zu erledigen:[/] {', '.join(failed)}")
        return 1
    console.print("\n[green]Alles bereit.[/] Starte Jarvis mit: [bold]python -m jarvis[/]")
    return 0
