# Polymarket Arbitrage Scanner (read-only)

A small, honest tool that connects to Polymarket's live order books and **logs**
binary markets where buying both YES and NO shares would cost less than $1.00.
It does **not** trade, hold keys, or move money.

## Read this first — expectations

This scanner is the truthful version of the "arbitrage bot" idea. Use it to see
the real market before you risk a cent.

- **It cannot turn $100 into $1,000 in a day.** Nothing can do that reliably.
  Any "velocity model," "99.8% win rate," or compounding table that promises it
  is fabricated marketing, not math.
- **Real gaps are rare and tiny.** When YES_ask + NO_ask drops below $1.00, the
  difference is usually a fraction of a cent and disappears in milliseconds as
  faster bots take it. On a $100 bankroll, fees/slippage can eat the edge
  entirely.
- **"Robust" ≠ "profitable."** Careful engineering (Decimal math, retries,
  backoff) prevents *bugs*. It does nothing to create *profit* where there is no
  edge.

The point of running this is to replace hype with evidence. Watch
`opportunities.log` for a week and decide for yourself whether a real,
exploitable edge exists.

## How it works

1. Pulls active binary (two-outcome) markets from the Gamma API, sorted by
   liquidity.
2. For each market, fetches the CLOB order book for both tokens and reads the
   best (lowest) ask on each side.
3. Computes `edge = 1 - yes_ask - no_ask` using `decimal.Decimal` end to end.
4. Logs any market where `edge` exceeds your safety buffer.

All network calls retry with exponential backoff and never crash the loop.

## Run it

```bash
pip install -r requirements.txt
python scanner.py            # loop forever, ~top 200 markets, 30s between passes
python scanner.py --once     # single pass then exit
python scanner.py --limit 500 --buffer 0.01   # more markets, require >=1c edge
```

Output goes to stdout, `scanner.log` (everything), and `opportunities.log`
(only logged candidate gaps).

## Honest next steps

If — and only if — the scanner shows recurring, real gaps larger than realistic
fees/slippage, the responsible progression is:

1. **Paper trade**: simulate fills against the live book and track P&L with $0 at
   risk.
2. If paper trading is genuinely profitable over a meaningful sample, *then*
   consider a small, capital-limited live executor with conservative limits and
   a drawdown kill-switch — understanding it can still lose money.

Starting at step 1 (this scanner) costs nothing and risks nothing.
