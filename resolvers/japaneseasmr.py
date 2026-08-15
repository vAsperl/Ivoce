"""Resolve public JapaneseASMR post pages to their embedded audio URL.

The resolver deliberately only reads the public post HTML.  It does not solve
CAPTCHAs, authenticate, or follow download-gate links.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from http.cookiejar import CookieJar
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import urljoin, urlparse
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

try:
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover - fallback for minimal installations
    browser_requests = None


_HOSTS = {"japaneseasmr.com", "www.japaneseasmr.com"}
_AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac", ".m3u8")
_MAX_PAGE_BYTES = 4 * 1024 * 1024
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}
_MEDIA_URL_RE = re.compile(
    r"https?://[^\s\"'<>\\]+?(?:\.mp3|\.m4a|\.aac|\.ogg|\.opus|\.wav|\.flac|\.m3u8)(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)


error_logger = logging.getLogger("japaneseasmr.resolver")


class JapaneseASMRResolverError(RuntimeError):
    """Raised when a supported post cannot be resolved."""


@dataclass(frozen=True)
class ResolvedJapaneseASMR:
    stream_url: str
    webpage_url: str
    title: Optional[str] = None
    thumbnail: Optional[str] = None


def supports(url: str) -> bool:
    """Return whether *url* is an HTTP(S) JapaneseASMR URL."""
    try:
        parsed = urlparse(url.strip())
    except (AttributeError, ValueError):
        return False
    return parsed.scheme.lower() in {"http", "https"} and (parsed.hostname or "").lower() in _HOSTS


class _SameSiteRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not supports(newurl):
            raise JapaneseASMRResolverError("JapaneseASMR redirected to an unexpected site")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PageParser(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.media_urls: list[str] = []
        self.title: Optional[str] = None
        self.thumbnail: Optional[str] = None
        self._in_title = False
        self._title_parts: list[str] = []
        self._in_json_ld = False
        self._json_ld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs):
        values = {key.lower(): value for key, value in attrs if value is not None}
        tag = tag.lower()
        if tag in {"audio", "source"}:
            self._add_media(values.get("src") or values.get("data-src"))
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if key in {"og:title", "twitter:title"} and content and not self.title:
                self.title = content.strip()
            elif key in {"og:image", "twitter:image"} and content and not self.thumbnail:
                self.thumbnail = urljoin(self.page_url, content)
        elif tag == "title":
            self._in_title = True
        elif tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._in_json_ld = True
            self._json_ld_parts = []

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            if not self.title:
                self.title = "".join(self._title_parts).strip() or None
        elif tag == "script" and self._in_json_ld:
            self._in_json_ld = False
            self._parse_json_ld("".join(self._json_ld_parts))

    def handle_data(self, data: str):
        if self._in_title:
            self._title_parts.append(data)
        if self._in_json_ld:
            self._json_ld_parts.append(data)

    def _add_media(self, value: Optional[str]):
        if not value:
            return
        candidate = urljoin(self.page_url, html.unescape(value.strip()))
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https"} and candidate not in self.media_urls:
            self.media_urls.append(candidate)

    def _parse_json_ld(self, value: str):
        try:
            payload = json.loads(value)
        except (TypeError, ValueError):
            return
        items = payload if isinstance(payload, list) else [payload]
        while items:
            item = items.pop()
            if isinstance(item, list):
                items.extend(item)
            elif isinstance(item, dict):
                for key in ("contentUrl", "embedUrl"):
                    self._add_media(item.get(key))
                items.extend(item.values())


def _fetch_with_browser_transport(url: str, timeout: float):
    """Fetch with a Chrome-compatible TLS fingerprint when curl-cffi is installed."""
    try:
        response = browser_requests.get(
            url,
            headers=_BROWSER_HEADERS,
            impersonate="chrome",
            allow_redirects=True,
            timeout=timeout,
        )
    except Exception as exc:
        raise JapaneseASMRResolverError(f"Could not load JapaneseASMR page: {exc}") from exc
    if not supports(response.url):
        raise JapaneseASMRResolverError("JapaneseASMR redirected to an unexpected site")
    if response.status_code == 403:
        raise JapaneseASMRResolverError(
            "JapaneseASMR denied the request (HTTP 403). The site may require a "
            "CAPTCHA or may be blocking the Lavalink host's IP."
        )
    if response.status_code >= 400:
        raise JapaneseASMRResolverError(
            f"JapaneseASMR returned HTTP {response.status_code}"
        )
    content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise JapaneseASMRResolverError("JapaneseASMR returned a non-HTML page")
    body = response.content
    if len(body) > _MAX_PAGE_BYTES:
        raise JapaneseASMRResolverError("JapaneseASMR page is too large")
    return body, response.encoding or "utf-8", response.url


def _fetch_with_stdlib(url: str, timeout: float):
    opener = build_opener(_SameSiteRedirects(), HTTPCookieProcessor(CookieJar()))
    request = Request(url, headers=_BROWSER_HEADERS)
    try:
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise JapaneseASMRResolverError("JapaneseASMR returned a non-HTML page")
            body = response.read(_MAX_PAGE_BYTES + 1)
            if len(body) > _MAX_PAGE_BYTES:
                raise JapaneseASMRResolverError("JapaneseASMR page is too large")
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = response.geturl()
    except JapaneseASMRResolverError:
        raise
    except HTTPError as exc:
        if exc.code == 403:
            raise JapaneseASMRResolverError(
                "JapaneseASMR denied the request (HTTP 403). The site may require a "
                "browser verification or may be blocking the Lavalink host's IP."
            ) from exc
        raise JapaneseASMRResolverError(
            f"JapaneseASMR returned HTTP {exc.code}"
        ) from exc
    except Exception as exc:
        raise JapaneseASMRResolverError(f"Could not load JapaneseASMR page: {exc}") from exc
    return body, charset, final_url


def _resolve_sync(url: str, timeout: float) -> ResolvedJapaneseASMR:
    if browser_requests is not None:
        body, charset, final_url = _fetch_with_browser_transport(url, timeout)
    else:
        body, charset, final_url = _fetch_with_stdlib(url, timeout)

    text = body.decode(charset, errors="replace")
    parser = _PageParser(final_url)
    parser.feed(text)
    searchable_text = html.unescape(text).replace(r"\/", "/")
    for match in _MEDIA_URL_RE.findall(searchable_text):
        parser._add_media(match)
    if not parser.media_urls:
        raise JapaneseASMRResolverError(
            "No public audio source was found on this JapaneseASMR page"
        )
    stream_url = next(
        (item for item in parser.media_urls if urlparse(item).path.lower().endswith(_AUDIO_EXTENSIONS)),
        parser.media_urls[0],
    )
    return ResolvedJapaneseASMR(stream_url, final_url, parser.title, parser.thumbnail)


async def resolve(url: str, timeout: float = 15.0) -> Optional[ResolvedJapaneseASMR]:
    """Resolve a supported post without blocking the Discord event loop.

    Returns ``None`` for URLs owned by other sites.
    """
    if not supports(url):
        return None
    return await asyncio.to_thread(_resolve_sync, url.strip(), timeout)
