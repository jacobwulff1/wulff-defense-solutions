#!/usr/bin/env python3
"""
Polymarket binary-market arbitrage SCANNER (read-only, no trading).

For each active binary market it fetches the live CLOB order books for the YES
and NO tokens and checks the classic "buy both sides" relationship:

    best_ask(YES) + best_ask(NO) < 1.00

If you could buy one YES share and one NO share for less than $1.00 combined,
the pair redeems for exactly $1.00 at resolution, so the gap is (in principle)
risk-free profit. This script only *detects and logs* those gaps. It never sends
an order and never touches a wallet or private key.

There is no configuration of this program that turns $100 into $1000 in a day;
if you see a claim like that, it is fiction. Run it for a few days and you will
see for yourself that real gaps are rare and tiny.

Usage:
    pip install -r requirements.txt
    python scanner.py                 # scan top markets by liquidity, loop forever
    python scanner.py --once          # single pass then exit
    python scanner.py --limit 500     # scan more markets per pass
    python scanner.py --buffer 0.01   # require >=1c edge after buffer to log
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

import httpx

from pmclob import ONE, best_ask, asks, dec, fetch_markets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("scanner.log")],
)
log = logging.getLogger("scanner")

opp_log = logging.getLogger("opportunities")
opp_log.setLevel(logging.INFO)
opp_log.addHandler(logging.FileHandler("opportunities.log"))
opp_log.propagate = False


async def scan_once(client, markets, buffer: Decimal) -> int:
    found = 0
    for m in markets:
        yes = best_ask(await asks(client, m.yes_token_id))
        no = best_ask(await asks(client, m.no_token_id))
        if yes is None or no is None:
            continue
        edge = ONE - (yes + no)  # positive => buying both costs < $1
        if edge > buffer:
            found += 1
            msg = (
                f"ARB? edge={edge:+.4f} (yes_ask={yes} + no_ask={no} = {yes + no}) "
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
        log.info("scanning %d markets; logging pairs where (1 - yes_ask - no_ask) > %s",
                 len(markets), buffer)
        while True:
            found = await scan_once(client, markets, buffer)
            log.info("pass complete: %d candidate opportunit%s above buffer",
                     found, "y" if found == 1 else "ies")
            if once:
                return
            await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket arbitrage scanner (read-only)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--buffer", type=str, default="0.005",
                        help="minimum edge in dollars after safety buffer to log")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()

    buffer = dec(args.buffer) or Decimal("0.005")
    try:
        asyncio.run(run(args.limit, buffer, args.once, args.interval))
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
