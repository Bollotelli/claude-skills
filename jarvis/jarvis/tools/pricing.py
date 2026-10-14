"""Price calculation for 3D-printed products: what one piece costs and what is left after fees.

Material, electricity, wear and a misprint buffer are estimated from grams and
print hours; platform fees are applied to the price plus the shipping the buyer
pays. Kleinunternehmer (§ 19 UStG) charge no VAT, so none is calculated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

WEAR_PER_HOUR = 0.10  # nozzle, build plate, belts: rough estimate per print hour
MISPRINT_BUFFER = 0.10  # 10 % extra material and power for failed prints


@dataclass(frozen=True)
class Platform:
    label: str
    where: str  # "auf Etsy", "im eigenen Shop"
    rate: float  # share of price + shipping
    fixed: float  # euros per order
    note: str


# Checked against the platforms' fee pages; see README ("Druck-Kalkulation") for sources.
PLATFORMS = {
    "etsy": Platform("Etsy", "auf Etsy", 0.065 + 0.04, 0.30 + 0.19, "6,5 % Transaktion + 4 % + 0,30 € Zahlung + 0,19 € Einstellgebühr"),
    "ebay": Platform("eBay (gewerblich)", "auf eBay", 0.11, 0.35, "11 % Provision + 0,35 € je Bestellung"),
    "shop": Platform("eigener Shop", "im eigenen Shop", 0.0299, 0.39, "nur Zahlungsgebühr wie bei PayPal, ohne Monatskosten"),
    "direkt": Platform("Direktverkauf", "beim Direktverkauf", 0.0, 0.0, "keine Gebühren"),
}


def euro(value: float) -> str:
    return f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def nice_price(value: float) -> float:
    """Round up to the next x,90 (e.g. 7,12 -> 7,90; 7,95 -> 8,90)."""
    return math.ceil(value - 0.9 - 1e-9) + 0.9


@dataclass
class Costs:
    material: float
    power: float
    wear: float
    buffer: float
    packaging: float
    postage: float

    @property
    def production(self) -> float:
        return self.material + self.power + self.wear + self.buffer

    @property
    def total(self) -> float:
        return self.production + self.packaging + self.postage


class PriceCalculator:
    def __init__(self, filament_eur_per_kg: float, electricity_eur_per_kwh: float, printer_watts: float,
                 packaging: float):
        self.filament = filament_eur_per_kg
        self.electricity = electricity_eur_per_kwh
        self.watts = printer_watts
        self.packaging = packaging

    def costs(self, grams: float, hours: float, purge_grams: float = 0.0, postage: float = 0.0) -> Costs:
        material = (grams + purge_grams) * self.filament / 1000
        power = hours * self.watts / 1000 * self.electricity
        return Costs(material, power, hours * WEAR_PER_HOUR, (material + power) * MISPRINT_BUFFER,
                     self.packaging, postage)

    @staticmethod
    def fees(platform: Platform, price: float, shipping: float) -> float:
        return (price + shipping) * platform.rate + platform.fixed

    def suggest(self, platform: Platform, costs: Costs, shipping: float, profit: float) -> float:
        """Price at which `profit` euros are left after costs and fees."""
        return (profit + costs.total + platform.fixed) / (1 - platform.rate) - shipping

    def report(self, grams: float, hours: float, platform_key: str = "etsy", price: float | None = None,
               shipping: float = 0.0, postage: float = 0.0, purge_grams: float = 0.0,
               work_minutes: float = 10.0, target_profit: float = 3.0) -> str:
        platform = PLATFORMS[platform_key]
        costs = self.costs(grams, hours, purge_grams, postage)
        lines = [
            f"Herstellung: {euro(costs.production)} (Filament {euro(costs.material)}"
            + (f" inkl. {purge_grams:g} g Spülabfall" if purge_grams else "")
            + f", Strom {euro(costs.power)}, Verschleiß {euro(costs.wear)}, Fehldruck-Puffer {euro(costs.buffer)}).",
            f"Verpackung {euro(costs.packaging)}" + (f", Porto {euro(postage)}" if postage else "") + ".",
        ]
        suggestion = nice_price(self.suggest(platform, costs, shipping, target_profit))
        if price is None:
            price = suggestion
            lines.append(f"Preisvorschlag {platform.where}: {euro(price)} (damit bleiben mindestens {euro(target_profit)}).")
        fees = self.fees(platform, price, shipping)
        profit = price + shipping - fees - costs.total
        lines.append(
            f"Bei {euro(price)}" + (f" + {euro(shipping)} Versand" if shipping else "")
            + f" {platform.where}: Gebühren {euro(fees)} ({platform.note}), Gewinn {euro(profit)}."
        )
        if hours > 0:
            lines.append(f"Das sind {euro(profit / hours)} pro Druckstunde (der Drucker arbeitet allein).")
        if work_minutes > 0:
            lines.append(f"Für deine Arbeitszeit von {work_minutes:g} Minuten (Packen, Nachricht, Versand): "
                         f"{euro(profit / work_minutes * 60)} pro Stunde.")
        if profit < target_profit and price != suggestion:
            lines.append(f"Für {euro(target_profit)} Gewinn müsste der Preis bei {euro(suggestion)} liegen.")
        lines.append("Annahmen: Kleinunternehmer ohne Umsatzsteuer, Schätzwerte für Strom und Verschleiß.")
        return "\n".join(lines)
