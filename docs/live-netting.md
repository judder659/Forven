# Live netting engine (shared coins)

Hyperliquid keeps one net position per coin per account. Without this engine
Forven lets one live strategy hold a coin at a time and refuses every other
entry on it. With the engine on, any number of live strategies can trade the
same coin, long or short, at the same time.

It is **off by default**. Turn it on from the Risk page ("Shared coins"), by
typing `NETTING`. It can only be switched while no live position is open, and it
runs on the main wallet only (it stays off while direction books are on).

## What changes when it is on

| | Engine off | Engine on |
|---|---|---|
| Strategies per coin | One | Any number |
| What the exchange holds | That strategy's position | The sum of every strategy's trade on the coin |
| Each trade's stop and target | Resting orders on the exchange | Run by Forven: a touched level sends that trade's close order |
| Protection if Forven is not running | The trade's own stop order | One disaster stop per coin |
| Capital per strategy | Balance / number of live strategies | One slot, taken only while in a trade |
| Books checked against the exchange | Trade by trade | By net position per coin |

### Orders

Each live trade stays its own row with its own size, entry, stop and target. An
entry or exit sends one order for that strategy's size and the exchange nets it.
A long and a short on the same coin offset each other, so closing one of them
can move the exchange position through zero. Only that order is sent; the other
strategies' trades are not touched.

### Stops and the disaster stop

One exchange position cannot carry a stop per strategy, so Forven's mark watcher
enforces each live trade's stop, trailing stop and target, the same way it
already does for paper trades. The closed-bar check in the scanner remains as a
backstop.

Forven must be running for those to act. For the case where it is not, each coin
has one **disaster stop** resting on the exchange for the whole net position. It
sits just beyond the furthest strategy stop on the side the net position is on
(default gap 0.5%): below the lowest stop when the coin is net long, above the
highest when net short. Every strategy stop is nearer, so while Forven is
running the disaster stop never fires. It is replaced whenever a trade opens,
closes or trails its stop, always new-before-old.

If it does fire, the exchange has closed the whole net position. Selling the net
of three longs and one short is the same trade as closing all four at that
price, so Forven closes every row on that coin at the fill price and tells you.

### Slots

A slot is one full-size position. A probation trade takes its share of one (25%
or 50%), so full-size and probation strategies run side by side.

The slot count is calculated per install:

- **History asks for** the number of slots that covers 95% of the time the live
  strategies have been in a trade together (last 180 days), plus one spare.
- **The balance can carry** at most `balance x smallest size in use / $10`
  slots, because an order at the smallest size must clear the exchange's $10
  minimum.

The smaller of the two is used. It is recalculated when the live cohort or a
strategy's size changes, and at most hourly otherwise. It never resizes a trade
that is already open. You can set the count yourself on the Risk page.

When every slot is in use, a new live entry is skipped and shown as such.

### Books versus the exchange

Every 20 seconds Forven compares what its live trades add up to on each coin
with what the exchange holds, and again in the regular reconcile.

A difference seen on two checks in a row pauses new entries on that coin and
raises an alert. **Forven never trades on its own to correct it.** The Risk page
offers two actions:

- **Trade the exchange to match**: send the order that makes the exchange hold
  what the live trades add up to.
- **Close these trades in Forven**: when the exchange holds none of the coin
  (for example you closed it on the exchange yourself), close the coin's live
  trades in Forven at the current price, with no order.

Exits keep working during a difference, with one rule: a close never opens
exposure the books cannot account for. It reduces what is really there and books
the rest out without an order.

## Limits to know about

- **Forven must be running** for strategy stops and targets. Use
  `./launcher.sh autostart on` (Linux) so it starts with the machine.
- **Stops fill at market.** A stop run by Forven is a market order sent when the
  level is touched, so its fill can be a little worse than a resting stop's.
- **Funding** is charged by the exchange on the net position. It is not divided
  between the strategies' trades, so per-trade PnL excludes it.
- **The account limits still apply**: per-trade risk, the open-risk budget, the
  per-coin and per-group exposure caps, the daily loss limit and the kill
  switch. The position cap counts coins.
- **Main wallet only.** Sub-account (direction book) trading keeps the old rules.

## API

- `GET /api/live/netting`: status, slots, each coin's trades, net position and
  disaster stop.
- `POST /api/live/netting` `{"enabled": true, "confirm": "NETTING"}`: switch.
- `POST /api/live/netting/settings`
  `{"slots_override": 0, "slot_coverage_pct": 95, "disaster_gap_pct": 0.5}`.
- `POST /api/live/netting/check`: compare with the exchange now.
- `POST /api/live/netting/resolve`
  `{"coin": "SOL", "action": "trade_to_books" | "close_books"}`.

All need operator access.
