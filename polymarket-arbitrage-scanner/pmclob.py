"""
Shared, read-only Polymarket CLOB helpers used by scanner.py and paper_bot.py.

No trading, no keys, no funds. Pure market-data access with Decimal math.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Optional

import httpx

getcontext().prec = 28

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

ONE = Decimal("1")
ZERO = Decimal("0")

log = logging.getLogger("pmclob")


@dataclass(frozen=True)
class Market:
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str


@dataclass(frozen=True)
class Level:
    price: Decimal
    size: Decimal


def dec(value) -> Optional[Decimal]:
    """Parse anything into Decimal via str() (never via float), or None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def get_json(client: httpx.AsyncClient, url: str, **kwargs):
    """GET with exponential backoff on 429 / transient errors. Returns parsed
    JSON or None. Never raises into the caller's loop."""
    delay = Decimal("1")
    for attempt in range(5):
        try:
            resp = await client.get(url, timeout=20, **kwargs)
            if resp.status_code == 429:
                log.warning("429 rate limited on %s; backing off %ss", url, delay)
                await asyncio.sleep(float(delay))
                delay *= 2
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            log.warning("request failed (%s) attempt %d: %s", url, attempt + 1, exc)
            await asyncio.sleep(float(delay))
            delay *= 2
    log.error("giving up on %s after retries", url)
    return None


async def fetch_markets(client: httpx.AsyncClient, limit: int) -> list[Market]:
    """Pull active, non-closed two-outcome (YES/NO) markets from Gamma API."""
    markets: list[Market] = []
    offset = 0
    page = 100
    while len(markets) < limit:
        params = {
            "active": "true",
            "closed": "false",
            "limit": str(page),
            "offset": str(offset),
            "order": "liquidityNum",
            "ascending": "false",
        }
        data = await get_json(client, f"{GAMMA_API}/markets", params=params)
        if not data:
            break
        for m in data:
            token_ids_raw = m.get("clobTokenIds")
            outcomes_raw = m.get("outcomes")
            if not token_ids_raw or not outcomes_raw:
                continue
            try:
                token_ids = json.loads(token_ids_raw)
                outcomes = json.loads(outcomes_raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if len(token_ids) != 2 or len(outcomes) != 2:
                continue
            markets.append(
                Market(
                    question=m.get("question", m.get("slug", "?")),
                    slug=m.get("slug", "?"),
                    yes_token_id=str(token_ids[0]),
                    no_token_id=str(token_ids[1]),
                )
            )
            if len(markets) >= limit:
                break
        if len(data) < page:
            break
        offset += page
    log.info("loaded %d binary markets", len(markets))
    return markets


async def asks(client: httpx.AsyncClient, token_id: str) -> list[Level]:
    """Ascending-by-price ask levels (price, size) for a token, or []."""
    data = await get_json(client, f"{CLOB_API}/book", params={"token_id": token_id})
    if not data:
        return []
    levels: list[Level] = []
    for a in data.get("asks") or []:
        p, s = dec(a.get("price")), dec(a.get("size"))
        if p is not None and s is not None and s > ZERO:
            levels.append(Level(p, s))
    levels.sort(key=lambda lv: lv.price)
    return levels


def best_ask(levels: list[Level]) -> Optional[Decimal]:
    return levels[0].price if levels else None


def fill_cost(levels: list[Level], shares: Decimal) -> Optional[Decimal]:
    """Walk the ask book to buy `shares`. Returns total cost (Decimal) or None
    if the book lacks enough depth to fill fully (no partial fills)."""
    remaining = shares
    cost = ZERO
    for lv in levels:
        take = lv.size if lv.size < remaining else remaining
        cost += take * lv.price
        remaining -= take
        if remaining <= ZERO:
            return cost
    return None  # not enough depth to fill the whole size
