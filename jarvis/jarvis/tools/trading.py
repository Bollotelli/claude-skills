"""Trading notebook for EA FC Ultimate Team, stored in ``~/.jarvis/trading.json``.

Jarvis never touches the game or EA's web app (automated trading breaks EA's
rules and gets accounts banned). You tell him what you bought and the prices
you see; he keeps the book, applies the 5 % EA tax and reports profit.
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from .storage import JsonStore

EA_TAX = 0.05  # EA keeps 5 % of every transfer-market sale


def coins(value: float) -> str:
    """15000 -> '15.000'"""
    return f"{round(value):,}".replace(",", ".")


def signed(value: float) -> str:
    return ("+" if value >= 0 else "−") + coins(abs(value))


def net_sale(price: float) -> float:
    return price * (1 - EA_TAX)


def break_even(buy_price: float) -> int:
    return math.ceil(buy_price / (1 - EA_TAX))


class TradeBook:
    def __init__(self, path: Path):
        self.store = JsonStore(path)

    def add(self, item: str, buy_price: int, quantity: int = 1, target: int | None = None, note: str = "") -> dict:
        with self.store.lock:
            trades = self.store.load()
            trade = {
                "id": JsonStore.next_id(trades),
                "item": item.strip(),
                "quantity": quantity,
                "buy_price": buy_price,
                "target": target,
                "current_price": None,
                "sold_price": None,
                "note": note.strip(),
                "bought": datetime.now().isoformat(timespec="minutes"),
            }
            trades.append(trade)
            self.store.save(trades)
            return trade

    def update(self, trade_id: int, current_price: int | None = None, sold_price: int | None = None) -> dict:
        with self.store.lock:
            trades = self.store.load()
            for trade in trades:
                if trade.get("id") == trade_id:
                    if current_price is not None:
                        trade["current_price"] = current_price
                    if sold_price is not None:
                        trade["sold_price"] = sold_price
                        trade["sold"] = datetime.now().isoformat(timespec="minutes")
                    self.store.save(trades)
                    return trade
            raise KeyError(trade_id)

    @staticmethod
    def describe(trade: dict) -> str:
        qty, buy = trade["quantity"], trade["buy_price"]
        head = f"#{trade['id']} {trade['item']}" + (f" ({qty}×)" if qty != 1 else "")
        parts = [f"gekauft für {coins(buy)}", f"Break-even {coins(break_even(buy))}"]
        if trade.get("sold_price") is not None:
            profit = (net_sale(trade["sold_price"]) - buy) * qty
            return f"{head}: verkauft für {coins(trade['sold_price'])}, Gewinn nach EA-Steuer {signed(profit)}"
        if trade.get("target"):
            parts.append(f"Ziel {coins(trade['target'])} ({signed((net_sale(trade['target']) - buy) * qty)})")
        if trade.get("current_price") is not None:
            parts.append(f"aktuell {coins(trade['current_price'])} ({signed((net_sale(trade['current_price']) - buy) * qty)})")
        if trade.get("note"):
            parts.append(trade["note"])
        return f"{head}: " + ", ".join(parts)

    def overview(self, include_closed: bool = False) -> str:
        trades = self.store.load()
        open_trades = [t for t in trades if t.get("sold_price") is None]
        closed = [t for t in trades if t.get("sold_price") is not None]
        lines = [self.describe(t) for t in open_trades]
        if include_closed:
            lines += [self.describe(t) for t in closed]
        if not lines:
            lines.append("Noch keine offenen Investments.")
        invested = sum(t["buy_price"] * t["quantity"] for t in open_trades)
        realized = sum((net_sale(t["sold_price"]) - t["buy_price"]) * t["quantity"] for t in closed)
        lines.append(
            f"Summe: {len(open_trades)} offen, eingesetzt {coins(invested)} Münzen; "
            f"realisierter Gewinn nach 5 % EA-Steuer {signed(realized)} Münzen."
        )
        return "\n".join(lines)
