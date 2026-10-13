"""German date/time phrasing (independent of the operating system's locale)."""

from __future__ import annotations

from datetime import datetime

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def spoken(dt: datetime) -> str:
    """e.g. 'Montag, 13. Oktober 2026, 22:58 Uhr'"""
    return f"{WEEKDAYS[dt.weekday()]}, {dt.day}. {MONTHS[dt.month - 1]} {dt.year}, {dt:%H:%M} Uhr"


def context_note(dt: datetime, zone_name: str) -> str:
    """The per-turn time note Jarvis receives as a system message."""
    offset = dt.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "lokale Zeit"
    return (
        f"Aktuelle Zeit: {spoken(dt)} ({zone_name}, {offset}). "
        f"ISO: {dt.isoformat(timespec='seconds')}"
    )
