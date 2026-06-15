#!/usr/bin/env python3
"""
Polymarket binary-market arbitrage SCANNER (read-only, no trading).

What it actually does
---------------------
For each active binary market it fetches the live CLOB order books for the YES
and NO tokens and checks the classic "buy both sides" relationship:

    best_ask(YES) + best_ask(NO) < 1.00

If you could buy one YES share and one NO share for less than $1.00 combined,
the pair is guaranteed to redeem for exactly $1.00 at resolution, so the gap is
(in principle) risk-free profit. This script only *detects and logs* those gaps.
It never sends an order and never touches a wallet or private key.

Why it is read-only on purpose
------------------------------
Run it for a few days first. You will see for yourself that real gaps are rare,
tiny (usually a fraction of a cent after the safety buffer), and often vanish
before you could act. That is the honest picture of this trade. There is no
configuration of this program that turns $100 into $1000 in a day; if you see a
claim like that, it is fiction.

Usage
-----
    pip install -r requirements.txt
    python scanner.py                 # scan top markets by liquidity, loop forever
    python scanner.py --once          # single pass then exit
    python scanner.py --limit 500     # scan more markets per pass
    python scanner.py --buffer 0.01   # require >=1c edge after buffer to log

All numbers use decimal.Decimal end to end. No floats in the money path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal, getcontext
from typing import Optional

import httpx

# Plenty of precision for price math; we never need more than a few places.
getcontext().prec = 28

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

ONE = Decimal("1")

# ---------------------------------------------------------------------------
# Logging: human-readable to stdout, opportunities also appended to a file.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("scanner.log"),
    ],
)
log = logging.getLogger("scanner")

# Opportunities get their own file so you can grep history cleanly.
opp_log = logging.getLogger("opportunities")
opp_log.setLevel(logging.INFO)
opp_log.addHandler(logging.FileHandler("opportunities.log"))
opp_log.propagate = False


@dataclass(frozen=True)
class Market:
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str


def _dec(value) -> Optional[Decimal]:
    """Parse anything into a Decimal via str() (never via float), or None."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs):
    """GET with exponential backoff on 429 / transient errors. Returns parsed
    JSON or None. Never raises into the main loop."""
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
    """Pull active, non-closed binary markets from the Gamma API."""
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
        data = await _get_json(client, f"{GAMMA_API}/markets", params=params)
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
            # We only handle clean two-outcome (YES/NO) binary markets.
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


async def best_ask(client: httpx.AsyncClient, token_id: str) -> Optional[Decimal]:
    """Lowest ask price for a token from the CLOB order book, or None if the
    book is empty / unavailable."""
    data = await _get_json(client, f"{CLOB_API}/book", params={"token_id": token_id})
    if not data:
        return None
    asks = data.get("asks") or []
    prices = [p for p in (_dec(a.get("price")) for a in asks) if p is not None]
    return min(prices) if prices else None


async def scan_once(client: httpx.AsyncClient, markets: list[Market], buffer: Decimal) -> int:
    """One full pass. Returns the number of opportunities logged."""
    found = 0
    for m in markets:
        yes = await best_ask(client, m.yes_token_id)
        no = await best_ask(client, m.no_token_id)
        if yes is None or no is None:
            continue
        total = yes + no
        edge = ONE - total  # positive => buying both costs < $1
        if edge > buffer:
            found += 1
            msg = (
                f"ARB? edge={edge:+.4f} (yes_ask={yes} + no_ask={no} = {total}) "
                f"| {m.question} | https://polymarket.com/market/{m.slug}"
            )
            log.info(msg)
            opp_log.info(msg)
    return found


async def run(limit: int, buffer: Decimal, once: bool, interval: int) -> None:
    headers = {"User-Agent": "pm-arb-scanner/1.0 (read-only)"}
    async with httpx.AsyncClient(headers=headers) as client:
        markets = await fetch_markets(client, limit)
        if not markets:
            log.error("no markets loaded; check network access to Polymarket APIs")
            return
        log.info(
            "scanning %d markets; logging pairs where (1 - yes_ask - no_ask) > %s",
            len(markets),
            buffer,
        )
        while True:
            found = await scan_once(client, markets, buffer)
            log.info("pass complete: %d candidate opportunit%s above buffer",
                     found, "y" if found == 1 else "ies")
            if once:
                return
            await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket arbitrage scanner (read-only)")
    parser.add_argument("--limit", type=int, default=200, help="max markets to scan per pass")
    parser.add_argument("--buffer", type=str, default="0.005",
                        help="minimum edge (in dollars) after safety buffer to log; "
                             "default 0.005 (half a cent) to absorb fees/slippage")
    parser.add_argument("--once", action="store_true", help="single pass then exit")
    parser.add_argument("--interval", type=int, default=30, help="seconds between passes")
    args = parser.parse_args()

    buffer = _dec(args.buffer) or Decimal("0.005")
    try:
        asyncio.run(run(args.limit, buffer, args.once, args.interval))
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
