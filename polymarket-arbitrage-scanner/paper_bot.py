#!/usr/bin/env python3
"""
Polymarket hands-off paper-trading bot.

Runs autonomously, simulates buying both sides of binary markets when
YES_ask + NO_ask < $1.00, and tracks running P&L — with $0 real money at risk.

How paper-fills work
--------------------
When an arb gap is detected, the bot simulates filling both legs at the live
ask prices, walking the order book depth to model realistic slippage. It deducts
a 0.02c (2%) taker fee on each leg (Polymarket's standard). The simulated
position is held until we next observe the market resolving (price -> $1.00 or
$0.00), at which point P&L is settled.

Usage:
    pip install -r requirements.txt
    python paper_bot.py                       # run forever, $100 starting bankroll
    python paper_bot.py --bankroll 500        # different starting balance
    python paper_bot.py --alloc 5             # max $ per trade (default $5)
    python paper_bot.py --buffer 0.008        # min edge required (default $0.008)
    python paper_bot.py --markets 300         # number of markets to watch
    python paper_bot.py --interval 20         # seconds between scans (default 30)
    python paper_bot.py --killswitch 85       # stop if equity < $85 (default)

Output:
    paper_bot.log        — everything
    paper_trades.json    — every simulated trade (open + closed)
    paper_summary.log    — periodic P&L summary lines
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from typing import Optional

import httpx

from pmclob import ONE, ZERO, asks, best_ask, dec, fetch_markets

getcontext().prec = 28

FEE_RATE = Decimal("0.02")   # 2% taker fee per leg
HEADERS = {"User-Agent": "pm-paper-bot/1.0 (simulation, no real trades)"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    fmt = "%(asctime)s %(levelname)s %(message)s"
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(logging.StreamHandler(sys.stdout))
    root.addHandler(logging.FileHandler("paper_bot.log"))

    summary = logging.FileHandler("paper_summary.log")
    summary.setLevel(logging.INFO)
    logging.getLogger("summary").addHandler(summary)
    logging.getLogger("summary").propagate = True

    return logging.getLogger("paper_bot")


log = _setup_logging()
summary_log = logging.getLogger("summary")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Trade:
    id: str
    market_question: str
    market_slug: str
    yes_token_id: str
    no_token_id: str
    yes_ask: str          # Decimal serialised as str
    no_ask: str
    fee_yes: str
    fee_no: str
    shares: str
    total_cost: str       # cost including fees
    opened_at: str
    closed_at: Optional[str] = None
    resolution: Optional[str] = None   # "YES" | "NO" | "INVALID"
    pnl: Optional[str] = None          # Decimal serialised as str
    status: str = "open"


@dataclass
class Book:
    trades: list[Trade] = field(default_factory=list)
    bankroll: str = "100"             # current simulated cash (Decimal as str)
    locked: str = "0"                 # cash tied up in open positions
    total_pnl: str = "0"
    trades_opened: int = 0
    trades_closed: int = 0
    wins: int = 0
    losses: int = 0

    def equity(self) -> Decimal:
        return dec(self.bankroll) + dec(self.locked)

    def save(self, path: str = "paper_trades.json") -> None:
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @classmethod
    def load(cls, path: str = "paper_trades.json") -> "Book":
        try:
            with open(path) as f:
                data = json.load(f)
            obj = cls()
            for k, v in data.items():
                if k != "trades":
                    setattr(obj, k, v)
            obj.trades = [Trade(**t) for t in data.get("trades", [])]
            return obj
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            return cls()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------
def _trade_id(slug: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{slug[:20]}-{ts}"


def _simulate_open(
    book: Book,
    market,
    yes_levels,
    no_levels,
    alloc: Decimal,
) -> Optional[Trade]:
    """
    Try to open a paper trade.  Returns the Trade or None if insufficient
    depth / balance / edge.
    """
    ya = best_ask(yes_levels)
    na = best_ask(no_levels)
    if ya is None or na is None:
        return None

    cost_per_share = ya + na
    if cost_per_share >= ONE:
        return None

    # How many shares can we buy within alloc, walking the book?
    # We want the largest integer (in 0.01 increments) where depth covers us.
    max_shares = (alloc / cost_per_share).quantize(Decimal("0.01"))
    if max_shares <= ZERO:
        return None

    # Walk actual depth for both legs at that size.
    from pmclob import fill_cost as _fill_cost
    yes_cost = _fill_cost(yes_levels, max_shares)
    no_cost = _fill_cost(no_levels, max_shares)
    if yes_cost is None or no_cost is None:
        return None   # book too thin for this size

    fee_yes = (yes_cost * FEE_RATE).quantize(Decimal("0.000001"))
    fee_no = (no_cost * FEE_RATE).quantize(Decimal("0.000001"))
    total_cost = yes_cost + no_cost + fee_yes + fee_no

    bankroll = dec(book.bankroll)
    if total_cost > bankroll:
        return None   # can't afford

    # Deduct from bankroll, add to locked.
    book.bankroll = str(bankroll - total_cost)
    book.locked = str(dec(book.locked) + total_cost)

    t = Trade(
        id=_trade_id(market.slug),
        market_question=market.question,
        market_slug=market.slug,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        yes_ask=str(ya),
        no_ask=str(na),
        fee_yes=str(fee_yes),
        fee_no=str(fee_no),
        shares=str(max_shares),
        total_cost=str(total_cost),
        opened_at=_now(),
    )
    book.trades.append(t)
    book.trades_opened += 1
    return t


def _check_resolution(yes_levels, no_levels) -> Optional[str]:
    """
    Heuristic: if one side's best ask is >= $0.98, that side has resolved YES.
    We detect this by the other side collapsing near $0.
    """
    ya = best_ask(yes_levels)
    na = best_ask(no_levels)
    if ya is None and na is None:
        return None
    if ya is not None and ya >= Decimal("0.98"):
        return "YES"
    if na is not None and na >= Decimal("0.98"):
        return "NO"
    return None


def _simulate_close(book: Book, trade: Trade, resolution: str) -> None:
    """
    Settle a position. Both YES+NO always redeem for exactly $1 combined at
    resolution (one token → $1, other → $0). Paper profit = $1 * shares - cost.
    """
    shares = dec(trade.shares)
    total_cost = dec(trade.total_cost)
    pnl = (ONE * shares) - total_cost   # redeems for $1 per share pair

    book.locked = str(dec(book.locked) - total_cost)
    book.bankroll = str(dec(book.bankroll) + total_cost + pnl)
    book.total_pnl = str(dec(book.total_pnl) + pnl)
    book.trades_closed += 1
    if pnl >= ZERO:
        book.wins += 1
    else:
        book.losses += 1

    trade.closed_at = _now()
    trade.resolution = resolution
    trade.pnl = str(pnl)
    trade.status = "closed"


# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------
async def run(
    bankroll: Decimal,
    alloc: Decimal,
    buffer: Decimal,
    markets_limit: int,
    interval: int,
    killswitch: Decimal,
) -> None:
    book = Book.load()
    # Only set starting bankroll if this is a fresh book.
    if not book.trades and dec(book.bankroll) == Decimal("100") and bankroll != Decimal("100"):
        book.bankroll = str(bankroll)

    log.info(
        "paper bot starting | equity=$%s | open=%d | alloc=$%s | buffer=$%s | killswitch=$%s",
        book.equity(), sum(1 for t in book.trades if t.status == "open"),
        alloc, buffer, killswitch,
    )

    # Track which markets we have open positions in (one position per market).
    open_slugs = {t.market_slug for t in book.trades if t.status == "open"}
    lock = asyncio.Lock()

    async with httpx.AsyncClient(headers=HEADERS) as client:
        markets = await fetch_markets(client, markets_limit)
        if not markets:
            log.error("no markets loaded — check network. exiting.")
            return

        pass_number = 0
        while True:
            pass_number += 1
            equity = book.equity()

            # Kill-switch.
            if equity < killswitch:
                log.warning(
                    "KILL-SWITCH: equity $%s below $%s — stopping.",
                    equity, killswitch,
                )
                book.save()
                return

            opened_this_pass = 0
            closed_this_pass = 0

            async with lock:
                for m in markets:
                    yes_lvls = await asks(client, m.yes_token_id)
                    no_lvls = await asks(client, m.no_token_id)

                    # --- Try to close any open position on this market ---
                    for t in book.trades:
                        if t.market_slug == m.slug and t.status == "open":
                            res = _check_resolution(yes_lvls, no_lvls)
                            if res:
                                _simulate_close(book, t, res)
                                open_slugs.discard(m.slug)
                                closed_this_pass += 1
                                log.info(
                                    "CLOSED [%s] pnl=$%s resolution=%s | %s",
                                    t.id, t.pnl, res, m.question,
                                )

                    # --- Try to open a new position if none open here ---
                    if m.slug not in open_slugs:
                        ya = best_ask(yes_lvls)
                        na = best_ask(no_lvls)
                        if ya is not None and na is not None:
                            edge = ONE - (ya + na)
                            if edge > buffer:
                                trade = _simulate_open(
                                    book, m, yes_lvls, no_lvls, alloc
                                )
                                if trade:
                                    open_slugs.add(m.slug)
                                    opened_this_pass += 1
                                    log.info(
                                        "OPENED [%s] edge=$%s cost=$%s shares=%s | %s",
                                        trade.id,
                                        str((ONE - (ya + na)).quantize(Decimal("0.0001"))),
                                        trade.total_cost,
                                        trade.shares,
                                        m.question,
                                    )

                book.save()

            # --- Summary every pass ---
            open_count = sum(1 for t in book.trades if t.status == "open")
            summary_log.info(
                "PASS %d | equity=$%s | pnl=$%s | open=%d | closed=%d | "
                "opened=%d | closed=%d | wins=%d losses=%d",
                pass_number,
                str(book.equity().quantize(Decimal("0.01"))),
                str(dec(book.total_pnl).quantize(Decimal("0.01"))),
                open_count,
                book.trades_closed,
                opened_this_pass,
                closed_this_pass,
                book.wins,
                book.losses,
            )

            await asyncio.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket paper-trading bot (no real money)")
    parser.add_argument("--bankroll", type=str, default="100",
                        help="starting simulated balance in dollars (default 100)")
    parser.add_argument("--alloc", type=str, default="5",
                        help="max dollars per paper trade (default 5)")
    parser.add_argument("--buffer", type=str, default="0.008",
                        help="min edge in dollars required to enter a position (default 0.008)")
    parser.add_argument("--markets", type=int, default=200,
                        help="number of top markets to watch (default 200)")
    parser.add_argument("--interval", type=int, default=30,
                        help="seconds between scans (default 30)")
    parser.add_argument("--killswitch", type=str, default="85",
                        help="stop if simulated equity drops below this (default 85)")
    args = parser.parse_args()

    try:
        asyncio.run(run(
            bankroll=dec(args.bankroll) or Decimal("100"),
            alloc=dec(args.alloc) or Decimal("5"),
            buffer=dec(args.buffer) or Decimal("0.008"),
            markets_limit=args.markets,
            interval=args.interval,
            killswitch=dec(args.killswitch) or Decimal("85"),
        ))
    except KeyboardInterrupt:
        log.info("stopped by user")


if __name__ == "__main__":
    main()
