"""Command line entry point: ``python -m jarvis``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings

EXAMPLES = """\
Beispiele:
  python -m jarvis                 sprechen per Enter-Taste (Push-to-Talk)
  python -m jarvis --wake          freihändig: „Hey Jarvis, …“
  python -m jarvis --text          tippen statt sprechen
  python -m jarvis check           Einrichtung prüfen
  python -m jarvis sag "Hallo"     Stimme testen
  python -m jarvis stimmen         Stimmen zum Ausprobieren auflisten
  python -m jarvis selbsttest      Offline-Selbsttest (ohne API-Kosten)
"""
SAMPLE = "Guten Tag. Ich bin Jarvis, Ihr persönlicher Assistent. Alle Systeme sind bereit."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m jarvis",
        description="J.A.R.V.I.S. – dein persönlicher Sprachassistent mit Claude.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--text", action="store_true", help="tippen statt sprechen")
    mode.add_argument("--wake", action="store_true", help="freihändig: auf „Hey Jarvis“ hören")
    parser.add_argument("--stumm", "--mute", dest="mute", action="store_true", help="Antworten nur anzeigen, nicht vorlesen")
    sub = parser.add_subparsers(dest="command", metavar="befehl")
    check = sub.add_parser("check", aliases=["doctor"], help="Einrichtung prüfen")
    check.add_argument("--online", action="store_true", help="auch den API-Schlüssel bei Claude prüfen (kostenlos)")
    say = sub.add_parser("sag", aliases=["say"], help="Stimme testen")
    say.add_argument("text", nargs="*", help="was Jarvis sagen soll")
    say.add_argument("--stimme", help="andere edge-Stimme ausprobieren, z. B. de-DE-ConradNeural")
    say.add_argument("--datei", help="als WAV-Datei speichern statt abspielen")
    sub.add_parser("stimmen", aliases=["voices"], help="deutsche und britische Stimmen auflisten")
    sub.add_parser("selbsttest", aliases=["selftest"], help="Offline-Selbsttest (ohne API-Kosten)")
    return parser


def cmd_say(settings: Settings, args: argparse.Namespace) -> int:
    from .audio import Player
    from .tts import create_tts, wav_bytes

    if args.stimme:
        settings.tts, settings.edge_voice = "edge", args.stimme
    tts = create_tts(settings)
    if tts is None:
        print("Die Sprachausgabe ist ausgeschaltet (JARVIS_TTS=none).")
        return 1
    clip = tts.synthesize(" ".join(args.text) or SAMPLE)
    if args.datei:
        Path(args.datei).write_bytes(wav_bytes(clip))
        print(f"Gespeichert: {args.datei} ({clip.seconds:.1f} s)")
    else:
        Player(settings.output_device).play(clip)
    return 0


def cmd_voices() -> int:
    from rich.console import Console
    from rich.table import Table

    from .tts import EdgeTTS

    table = Table(title="Stimmen (edge, kostenlos)")
    for column in ("Name", "Geschlecht", "Charakter"):
        table.add_column(column)
    for voice in EdgeTTS.list_voices():
        traits = ", ".join((voice.get("VoiceTag") or {}).get("VoicePersonalities") or [])
        table.add_row(voice["ShortName"], voice.get("Gender", ""), traits)
    console = Console()
    console.print(table)
    console.print('Probehören: [bold]python -m jarvis sag --stimme de-DE-ConradNeural "Guten Abend."[/]')
    console.print("Gefällt dir eine? In jarvis/.env eintragen: JARVIS_EDGE_VOICE=<Name>")
    return 0


def run_assistant(settings: Settings, args: argparse.Namespace) -> int:
    from rich.console import Console

    from .app import THEME, Jarvis
    from .audio import AudioUnavailable
    from .tts import TTSError

    console = Console(theme=THEME)
    try:
        jarvis = Jarvis(settings, mute=args.mute, console=console)
    except (AudioUnavailable, TTSError) as exc:
        console.print(f"{exc}\nIch antworte vorerst nur als Text.", style="warn", markup=False)
        jarvis = Jarvis(settings, mute=True, console=console)
    try:
        if args.text:
            jarvis.run_text()
        elif args.wake:
            jarvis.run_wake_word()
        else:
            jarvis.run_push_to_talk()
    except AudioUnavailable as exc:
        console.print(f"{exc}\nIch wechsle in den Textmodus (tippen statt sprechen).", style="warn", markup=False)
        jarvis.run_text()
    return 0


def _friendly(func, *args) -> int:
    """Run a helper command; explain failures instead of printing a traceback."""
    try:
        return func(*args)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print(f"Fehler: {type(exc).__name__}: {exc}")
        print("Tipp: Internetverbindung prüfen und 'python -m jarvis check' ausführen.")
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    try:
        if args.command in ("check", "doctor"):
            from .doctor import main as doctor

            return doctor(settings, online=args.online)
        if args.command in ("sag", "say"):
            return _friendly(cmd_say, settings, args)
        if args.command in ("stimmen", "voices"):
            return _friendly(cmd_voices)
        if args.command in ("selbsttest", "selftest"):
            from .selftest import main as selftest

            return selftest()
        return run_assistant(settings, args)
    except KeyboardInterrupt:
        print("\nBis bald.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
