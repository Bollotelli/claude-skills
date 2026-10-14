"""Cardmarket sales book for card sellers, stored in ``~/.jarvis/cardmarket.json``.

Cardmarket accepts no new API applications, so orders come in by voice or
text. Jarvis keeps each order's shipping deadline (with a reminder), the
tracking number of the stamp and the monthly revenue after Cardmarket's
commission.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

from .. import timefmt
from .reminders import Reminders
from .storage import JsonStore

COMMISSION = 0.05  # Cardmarket fee table: 5 % of the article price, private and professional sellers
REGISTERED_FROM = 25.0  # Cardmarket requires registered shipping above this order value
REMIND_HOUR = 9  # shipping reminders go off at 9:00 on the deadline day
LONG_IN_TRANSIT = 14  # days after which a shipped order without arrival gets flagged

STATUS_WORDS = {
    "offen": "offen",
    "versendet": "versendet", "versandt": "versendet", "verschickt": "versendet", "raus": "versendet",
    "angekommen": "angekommen", "erhalten": "angekommen", "zugestellt": "angekommen",
    "storniert": "storniert", "abgebrochen": "storniert", "abgesagt": "storniert",
}
VIEWS = ("zu_versenden", "unterwegs", "alle")


def euro(value: float) -> str:
    """12.5 -> '12,50 €'"""
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def commission(price: float) -> float:
    """Cardmarket rounds its fee up to the next cent."""
    return math.ceil(round(price * COMMISSION * 100, 6)) / 100


def shipping_method(value: str) -> str:
    low = value.strip().lower()
    if "einschreib" in low:
        return "Einschreiben"
    if not low or "brief" in low:
        return "Brief"
    if "paket" in low or "päckchen" in low or "dhl" in low:
        return "Paket"
    return value.strip()


def status_word(value: str) -> str:
    status = STATUS_WORDS.get(value.strip().lower())
    if status is None:
        raise ValueError(f"Unbekannter Status „{value}“. Möglich: versendet, angekommen, storniert, offen.")
    return status


def count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def deadline(start: date, days: int) -> date:
    """Shipping deadline; a Sunday moves to Monday (no post collection)."""
    due = start + timedelta(days=days)
    return due + timedelta(days=1) if due.weekday() == 6 else due


def due_phrase(ship_by: date, today: date) -> str:
    days = (ship_by - today).days
    if days < 0:
        return f"überfällig seit {-days} Tag{'en' if days < -1 else ''}"
    if days == 0:
        return "heute fällig"
    if days == 1:
        return "morgen fällig"
    return f"fällig {timefmt.WEEKDAYS[ship_by.weekday()]}, {ship_by.day}.{ship_by.month}."


class SalesBook:
    def __init__(self, path: Path, reminders: Reminders, tz: tzinfo, ship_days: int = 2):
        self.store = JsonStore(path)
        self.reminders = reminders
        self.tz = tz
        self.ship_days = ship_days

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    # -- changes ------------------------------------------------------------

    def add(
        self,
        items: str,
        price: float,
        order_no: str = "",
        buyer: str = "",
        method: str = "Brief",
        shipping: float | None = None,
        ship_by: date | None = None,
        note: str = "",
    ) -> tuple[dict, list[str]]:
        """Record an order and set its shipping reminder; returns the order and warnings."""
        now = self._now()
        ship_by = ship_by or deadline(now.date(), self.ship_days)
        warnings = []
        method = shipping_method(method)
        if price > REGISTERED_FROM and method == "Brief":
            warnings.append(
                f"Über {euro(REGISTERED_FROM)} Warenwert schreibt Cardmarket versicherten Versand (Einschreiben) "
                "vor – prüf die Versandart in der Bestellung."
            )
        with self.store.lock:
            orders = self.store.load()
            order_no = order_no.strip()
            if order_no and any(o.get("order_no") == order_no for o in orders):
                raise ValueError(f"Die Cardmarket-Bestellung {order_no} ist schon eingetragen.")
            order = {
                "id": JsonStore.next_id(orders),
                "order_no": order_no,
                "buyer": buyer.strip(),
                "items": items.strip(),
                "price": round(price, 2),
                "shipping": round(shipping, 2) if shipping is not None else None,
                "postage": None,
                "method": method,
                "created": now.date().isoformat(),
                "ship_by": ship_by.isoformat(),
                "status": "offen",
                "tracking": "",
                "shipped": None,
                "reminder_id": None,
                "note": note.strip(),
            }
            remind_at = datetime(ship_by.year, ship_by.month, ship_by.day, REMIND_HOUR, tzinfo=self.tz)
            if ship_by < now.date():
                warnings.append("Die Versandfrist ist schon vorbei – am besten sofort verschicken.")
            else:
                if remind_at <= now:  # due today and 9:00 is over
                    remind_at = now + timedelta(hours=1)
                reminder = self.reminders.add(
                    f"Cardmarket: {self.label(order)} muss heute verschickt werden ({order['items']}).", remind_at
                )
                order["reminder_id"] = reminder["id"]
            orders.append(order)
            self.store.save(orders)
            return order, warnings

    def update(
        self,
        ref: str,
        status: str | None = None,
        tracking: str | None = None,
        postage: float | None = None,
        note: str | None = None,
    ) -> dict:
        tracking = (tracking or "").strip()
        if status is None and tracking:
            status = "versendet"
        with self.store.lock:
            orders = self.store.load()
            prefer = ("versendet", "offen") if status == "angekommen" else ("offen", "versendet")
            order = self.find(orders, ref, prefer)
            if tracking:
                order["tracking"] = tracking
            if postage is not None:
                order["postage"] = round(postage, 2)
            if note:
                order["note"] = f"{order['note']}; {note.strip()}" if order.get("note") else note.strip()
            if status:
                order["status"] = status
                if status == "versendet" and not order.get("shipped"):
                    order["shipped"] = self._now().date().isoformat()
                if status != "offen" and order.get("reminder_id"):
                    self.reminders.cancel(order["reminder_id"])
                    order["reminder_id"] = None
            self.store.save(orders)
            return order

    # -- lookups ------------------------------------------------------------

    @staticmethod
    def find(orders: list[dict], ref: str, prefer: tuple[str, ...] = ("offen", "versendet")) -> dict:
        """Find one order by Cardmarket number, Jarvis number, buyer or card name."""
        ref = ref.strip().lstrip("#").strip()
        for order in orders:
            if ref and order.get("order_no") == ref:
                return order
        if ref.isdigit():
            for order in orders:
                if order.get("id") == int(ref):
                    return order
        needle = ref.lower()
        hits = [
            o for o in orders
            if needle and (needle in o.get("buyer", "").lower() or needle in o.get("items", "").lower())
        ]
        for status in prefer:  # "Max hat bezahlt" means Max's open order, not last month's
            preferred = [o for o in hits if o.get("status") == status]
            if preferred:
                hits = preferred
                break
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise LookupError(f"Keine Cardmarket-Bestellung passt zu „{ref}“.")
        options = "; ".join(SalesBook.label(o) + f" ({o['items']})" for o in hits[:5])
        raise LookupError(f"Mehrere Bestellungen passen zu „{ref}“: {options}. Welche ist gemeint?")

    @staticmethod
    def label(order: dict) -> str:
        text = f"Bestellung #{order['id']}"
        if order.get("buyer"):
            text += f" an {order['buyer']}"
        return text

    def describe(self, order: dict, today: date | None = None) -> str:
        today = today or self._now().date()
        head = f"#{order['id']}"
        if order.get("order_no"):
            head += f" (Cardmarket {order['order_no']})"
        if order.get("buyer"):
            head += f" an {order['buyer']}"
        money = euro(order["price"])
        if order.get("shipping") is not None:
            money += f" + {euro(order['shipping'])} Versand"
        text = f"{head}: {order['items']}, {money}, {order['method']}"
        status = order.get("status")
        if status == "offen":
            text += f" – zu verschicken, {due_phrase(date.fromisoformat(order['ship_by']), today)}"
        elif status == "versendet":
            shipped = date.fromisoformat(order.get("shipped") or today.isoformat())
            text += f" – versendet am {shipped.day}.{shipped.month}."
            text += f", Sendungsnummer {order['tracking']}" if order.get("tracking") else ", ohne Sendungsnummer"
            if (today - shipped).days >= LONG_IN_TRANSIT:
                text += f", seit {(today - shipped).days} Tagen nicht als angekommen bestätigt"
        else:
            text += f" – {status}"
            if order.get("tracking"):
                text += f", Sendungsnummer {order['tracking']}"
        if order.get("note"):
            text += f" ({order['note']})"
        return text

    def listing(self, view: str = "zu_versenden", query: str = "") -> str:
        today = self._now().date()
        orders = self.store.load()
        needle = query.strip().lower()
        if needle:
            orders = [
                o for o in orders
                if any(needle in str(o.get(k) or "").lower() for k in ("buyer", "items", "order_no", "tracking"))
            ]
        if view == "zu_versenden":
            open_orders = sorted((o for o in orders if o.get("status") == "offen"), key=lambda o: o["ship_by"])
            if not open_orders:
                return "Nichts zu verschicken – alle Cardmarket-Bestellungen sind raus."
            overdue = sum(date.fromisoformat(o["ship_by"]) < today for o in open_orders)
            due_today = sum(date.fromisoformat(o["ship_by"]) == today for o in open_orders)
            head = f"{count(len(open_orders), 'Bestellung', 'Bestellungen')} zu verschicken"
            if overdue or due_today:
                head += f", davon {overdue} überfällig und {due_today} heute fällig"
            return "\n".join([head + ":"] + [self.describe(o, today) for o in open_orders])
        if view == "unterwegs":
            moving = sorted((o for o in orders if o.get("status") == "versendet"), key=lambda o: o.get("shipped") or "")
            if not moving:
                return "Keine Sendungen unterwegs."
            return "\n".join([f"{count(len(moving), 'Sendung', 'Sendungen')} unterwegs:"] + [self.describe(o, today) for o in moving])
        recent = sorted(orders, key=lambda o: o["id"], reverse=True)[:30]
        if not recent:
            return "Noch keine Cardmarket-Bestellungen eingetragen."
        return "\n".join(self.describe(o, today) for o in recent)

    def report(self, month: str = "") -> str:
        """Sales of one month ('YYYY-MM', default: this month) after Cardmarket's commission."""
        if month.strip():
            year, number = (int(part) for part in month.strip()[:7].split("-"))
            if not 1 <= number <= 12:
                raise ValueError(month)
        else:
            today = self._now().date()
            year, number = today.year, today.month
        prefix = f"{year:04d}-{number:02d}"
        orders = [o for o in self.store.load() if str(o.get("created", "")).startswith(prefix)]
        sold = [o for o in orders if o.get("status") != "storniert"]
        title = f"{timefmt.MONTHS[number - 1]} {year}"
        if not sold:
            return f"{title}: noch keine Cardmarket-Verkäufe eingetragen."
        revenue = sum(o["price"] for o in sold)
        shipping = sum(o.get("shipping") or 0 for o in sold)
        fees = sum(commission(o["price"]) for o in sold)
        with_postage = [o for o in sold if o.get("postage") is not None]
        postage = sum(o["postage"] for o in with_postage)
        open_count = sum(o.get("status") == "offen" for o in sold)
        lines = [
            f"{title}: {count(len(sold), 'Verkauf', 'Verkäufe')}"
            + (f", {open_count} davon noch zu verschicken" if open_count else "")
            + (f", {len(orders) - len(sold)} storniert" if len(orders) > len(sold) else "")
            + ".",
            f"Artikelwert {euro(revenue)}, Versand von Käufern {euro(shipping)}, "
            f"Cardmarket-Provision (5 %) {euro(fees)}.",
        ]
        if with_postage:
            lines.append(f"Porto {euro(postage)} (bei {len(with_postage)} von {len(sold)} Bestellungen eingetragen).")
        else:
            lines.append("Porto noch nicht eingetragen.")
        lines.append(
            f"Bleibt: {euro(revenue + shipping - fees - postage)} – ohne deinen Einkaufspreis der Karten."
        )
        return "\n".join(lines)
