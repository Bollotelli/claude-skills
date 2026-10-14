"""Weather via Open-Meteo (free, no API key): https://open-meteo.com"""

from __future__ import annotations

from datetime import date

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

WEEKDAYS_DE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]

# WMO weather interpretation codes -> German description
WMO_DE = {
    0: "klar", 1: "überwiegend klar", 2: "teilweise bewölkt", 3: "bedeckt",
    45: "Nebel", 48: "Nebel mit Reif",
    51: "leichter Nieselregen", 53: "Nieselregen", 55: "starker Nieselregen",
    56: "leichter gefrierender Nieselregen", 57: "gefrierender Nieselregen",
    61: "leichter Regen", 63: "Regen", 65: "starker Regen",
    66: "leichter gefrierender Regen", 67: "gefrierender Regen",
    71: "leichter Schneefall", 73: "Schneefall", 75: "starker Schneefall", 77: "Schneegriesel",
    80: "leichte Regenschauer", 81: "Regenschauer", 82: "heftige Regenschauer",
    85: "leichte Schneeschauer", 86: "starke Schneeschauer",
    95: "Gewitter", 96: "Gewitter mit leichtem Hagel", 99: "Gewitter mit Hagel",
}


def _get_json(url: str, params: dict) -> dict:
    import httpx2 as httpx  # ships with the anthropic SDK

    response = httpx.get(url, params=params, timeout=10.0)
    response.raise_for_status()
    return response.json()


class Weather:
    def __init__(self, default_location: str):
        self.default_location = default_location
        self._places: dict[str, dict] = {}

    def geocode(self, location: str) -> dict:
        key = location.strip().lower()
        if key in self._places:
            return self._places[key]
        name, _, hint = location.partition(",")
        data = _get_json(GEOCODE_URL, {"name": name.strip(), "count": 5, "language": "de", "format": "json"})
        results = data.get("results") or []
        if not results:
            raise LookupError(f"Ort nicht gefunden: {location}")
        hint = hint.strip().lower()
        place = next(
            (r for r in results if hint and hint in (r.get("country", "") + " " + r.get("country_code", "")).lower()),
            results[0],
        )
        self._places[key] = place
        return place

    def forecast(self, location: str = "", days: int = 1) -> str:
        place = self.geocode(location or self.default_location)
        data = _get_json(FORECAST_URL, {
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
            "timezone": "auto",
            "forecast_days": max(1, min(days, 7)),
        })
        return format_weather(place, data)


def _n(value) -> str:
    """Round a measurement for speaking; Open-Meteo sends null for missing data."""
    return "?" if value is None else f"{value:.0f}"


def format_weather(place: dict, data: dict) -> str:
    """Turn an Open-Meteo response into a compact German summary for Claude."""
    label = place.get("name", "?")
    if place.get("country"):
        label += f" ({place['country']})"
    lines = [label]
    cur = data.get("current") or {}
    if cur:
        lines.append(
            f"Jetzt: {_n(cur.get('temperature_2m'))} °C (gefühlt {_n(cur.get('apparent_temperature'))} °C), "
            f"{WMO_DE.get(cur.get('weather_code'), 'unbekannt')}, Wind {_n(cur.get('wind_speed_10m'))} km/h, "
            f"Luftfeuchte {_n(cur.get('relative_humidity_2m'))} %."
        )
    daily = data.get("daily") or {}
    days = daily.get("time") or []
    # "today" at the forecast location (timezone=auto), not on this computer
    today = date.fromisoformat(cur["time"][:10]) if cur.get("time") else date.today()

    def col(name: str) -> list:
        return daily.get(name) or [None] * len(days)

    for i, day in enumerate(days):
        d = date.fromisoformat(day)
        name = {0: "Heute", 1: "Morgen"}.get((d - today).days, WEEKDAYS_DE[d.weekday()])
        line = f"{name}: {WMO_DE.get(col('weather_code')[i], 'unbekannt')}, "
        line += f"{_n(col('temperature_2m_min')[i])} bis {_n(col('temperature_2m_max')[i])} °C"
        if col("precipitation_probability_max")[i] is not None:
            line += f", Regenrisiko {col('precipitation_probability_max')[i]} %"
        sunrise, sunset = col("sunrise")[i], col("sunset")[i]
        if sunrise and sunset:
            line += f", Sonnenaufgang {sunrise[-5:]}, Sonnenuntergang {sunset[-5:]}"
        lines.append(line + ".")
    return "\n".join(lines)
