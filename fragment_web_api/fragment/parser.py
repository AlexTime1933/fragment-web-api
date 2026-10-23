"""Parsing of Fragment HTML fragments (stdlib only)."""

from __future__ import annotations

import html
import json
import re
from decimal import Decimal, InvalidOperation

from .models import PremiumPrice, StarsPrice, WalletSession

_TAG_RE = re.compile(r"<[^>]+>")
_API_URL_RE = re.compile(r'"apiUrl"\s*:\s*"([^"]+)"')
_AJ_INIT_RE = re.compile(r"ajInit\((\{.*?\})\);", re.S)
_WALLET_INIT_RE = re.compile(r"Wallet\.init\((\{.*?\})\)", re.S)


def text(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def number(fragment: str) -> Decimal | None:
    """``'11,007<span class="mini-frac">.11</span>'`` -> ``Decimal('11007.11')``."""
    raw = text(fragment).replace(",", "").replace(" ", "").replace(" ", "").lstrip("$")
    m = re.search(r"-?\d+(?:\.\d+)?", raw)
    if not m:
        return None
    try:
        return Decimal(m.group(0))
    except InvalidOperation:
        return None


def api_hash(page_html: str) -> str | None:
    m = _API_URL_RE.search(page_html)
    if not m:
        return None
    url = m.group(1).replace("\\/", "/")
    h = re.search(r"hash=([0-9a-fA-F]+)", url)
    return h.group(1) if h else None


def aj_state(page_html: str) -> dict:
    """The ``state`` passed to ``ajInit`` (myselfResult, tonRate, ...)."""
    m = _AJ_INIT_RE.search(page_html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1)).get("state") or {}
    except ValueError:
        return {}


def wallet_session(page_html: str) -> WalletSession | None:
    m = _WALLET_INIT_RE.search(page_html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return WalletSession(
        address=data.get("address") or None,
        logged_in=bool(data.get("logged_in")),
        ton_proof=data.get("ton_proof") or None,
        version=int(data.get("version") or 1),
    )


def is_telegram_logged_in(page_html: str) -> bool:
    return "logout-link" in page_html and 'class="dropdown-menu-item icon-before icon-menu-disconnect logout-link"' in page_html


def _split_items(options_html: str, field: str) -> list[tuple[int, str]]:
    """Return ``(value, html_after_input)`` for each radio ``name=field``."""
    pattern = re.compile(rf'<input[^>]*name="{field}"[^>]*value="(\d+)"[^>]*>', re.S)
    matches = list(pattern.finditer(options_html))
    items = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(options_html)
        items.append((int(m.group(1)), options_html[m.end():end]))
    return items


def _div(chunk: str, cls_pattern: str) -> str | None:
    m = re.search(rf'<div class="[^"]*{cls_pattern}[^"]*">(.*?)</div>', chunk, re.S)
    return m.group(1) if m else None


def parse_stars_options(options_html: str) -> list[StarsPrice]:
    out = []
    for qty, chunk in _split_items(options_html, "stars"):
        usd = _div(chunk, r"tm-value icon-before icon-usd")
        ton = _div(chunk, r"icon-ton")
        ton_v = number(ton) if ton else None
        if ton_v is None:
            continue
        out.append(StarsPrice(quantity=qty, price_ton=ton_v, price_usd=number(usd) if usd else None))
    return out


def parse_stars_cur_price(cur_price_html: str, quantity: int) -> StarsPrice | None:
    usd = _div(cur_price_html, r"icon-usd")
    ton = _div(cur_price_html, r"icon-ton")
    ton_v = number(ton) if ton else None
    if ton_v is None:
        return None
    return StarsPrice(quantity=quantity, price_ton=ton_v, price_usd=number(usd) if usd else None)


def parse_premium_options(options_html: str) -> list[PremiumPrice]:
    out = []
    for months, chunk in _split_items(options_html, "months"):
        ton = _div(chunk, r"tm-value icon-before icon-ton")
        usd = _div(chunk, r"tm-radio-desc")
        badge = re.search(r'<span class="tm-radio-label-badge">(.*?)</span>', chunk, re.S)
        ton_v = number(ton) if ton else None
        if ton_v is None:
            continue
        out.append(
            PremiumPrice(
                months=months,
                price_ton=ton_v,
                price_usd=number(usd) if usd else None,
                discount=text(badge.group(1)) if badge else None,
            )
        )
    return out


def extract_options_html(page_html: str, kind: str) -> str | None:
    """Pull the options block out of a full /stars or /premium page."""
    marker = "js-stars-options" if kind == "stars" else "js-premium-options"
    i = page_html.find(marker)
    if i < 0:
        return None
    # the payment-method block follows the options; cut there
    end = page_html.find('name="payment_method"', i)
    return page_html[i:end if end > 0 else len(page_html)]


def strip_html(value: str | None) -> str | None:
    return text(value) if value else value
