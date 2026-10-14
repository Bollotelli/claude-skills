"""Free web search, news and page reading via ddgs (DuckDuckGo & co., no API key).

Used by the free local brain. The Claude brain has its own server-side search.
"""

from __future__ import annotations


class WebError(RuntimeError):
    pass


def _ddgs():
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise WebError("Die kostenlose Websuche fehlt: pip install ddgs") from exc
    return DDGS(timeout=10)


def _call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except WebError:
        raise
    except Exception as exc:  # ddgs raises DDGSException / TimeoutException for everything
        text = f"{type(exc).__name__} {exc}".lower()
        if "no results found" in text:
            return []
        if any(sign in text for sign in ("ratelimit", "202", "vqd", "document is empty")):
            raise WebError("Die Suche ist gerade überlastet. Bitte gleich noch einmal versuchen.") from exc
        if "timeout" in text:
            raise WebError("Die Suche hat zu lange gedauert. Ist das Internet verfügbar?") from exc
        raise WebError(f"Suche fehlgeschlagen ({exc})") from exc


class WebSearch:
    def __init__(self, region: str = "de-de", max_chars: int = 6000):
        self.region = region
        self.max_chars = max_chars

    def search(self, query: str, max_results: int = 5) -> str:
        results = _call(_ddgs().text, query, region=self.region, max_results=max_results) or []
        if not results:
            return "Keine Treffer."
        return "\n".join(
            f"{i}. {r.get('title', '')}: {r.get('body', '')} ({r.get('href', '')})"
            for i, r in enumerate(results, 1)
        )

    def news(self, query: str, max_results: int = 5) -> str:
        results = _call(_ddgs().news, query, region=self.region, max_results=max_results) or []
        if not results:
            return "Keine aktuellen Nachrichten gefunden."
        return "\n".join(
            f"{i}. [{r.get('date', '')[:10]}] {r.get('title', '')} – {r.get('source', '')}: "
            f"{r.get('body', '')} ({r.get('url', '')})"
            for i, r in enumerate(results, 1)
        )

    def read(self, url: str) -> str:
        if not url.startswith(("https://", "http://")):
            raise WebError("Nur http(s)-Adressen können gelesen werden.")
        page = _call(_ddgs().extract, url, fmt="text_markdown") or {}
        text = page.get("content", "")
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        text = text.strip()
        if not text:
            return "Die Seite enthält keinen lesbaren Text."
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + f"\n\n[... gekürzt: Seite hat {len(text)} Zeichen]"
        return text
