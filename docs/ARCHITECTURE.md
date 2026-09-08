# Architecture

## The shape of the problem

Cross-venue hedging is not "place two orders". It is:

1. Two venues that disagree about what a unit is, what a currency is, what
   financing costs, and when the market is open.
2. A second leg that can be rejected, partially filled, timed out or
   unreachable *after* the first leg has already taken exposure.
3. A book whose correctness decays continuously — prices move, an inverse
   leg's delta depends on its entry price, funding accrues.
4. A process that can die at any point, including between the legs.

Every structural decision below follows from one of those four.

## Layering

```
api/           FastAPI routers, WebSocket hub, RBAC.  Knows HTTP.
   │
service.py     HedgeLabService — the composition root.  Knows everything, once.
   │
   ├── hedge/ risk/ costs/     pure calculators
   ├── execution/              the state machine and its recovery paths
   ├── reconciliation/         three-way comparison, restart recovery
   ├── faults/                 injectable failure modes
   │
   ├── venues/                 TradingVenue ABC + paper adapters
   ├── marketdata/             deterministic simulator, FX
   ├── db/                     SQLAlchemy models, repositories, recorder
   │
domain/        InstrumentSpec, QuantityConverter, Exposure, Position, Order.
               Pure functions.  Imports nothing above it.
```

**The dependency rule**: `domain/` imports nothing from the layers above, and
`hedge/`, `risk/` and `costs/` never fetch anything — market data and account
state arrive as arguments. That is why 361 tests run in 22 seconds with no
database and no Docker: the calculation core has no I/O to mock.

**One composition root**: the CLI, the API and the scenario runner are all thin
shells over `HedgeLabService`. They cannot disagree about behaviour, because
there is only one implementation of each behaviour.

## Key decisions and why

### Decimal everywhere, strings on the wire

Binary floats break exchange rounding rules. `0.1 + 0.2 != 0.3` is a curiosity
in most software and a rejected order here. Every quantity and price is a
`Decimal`; every API response serialises them as **strings**; the dashboard
converts to `number` only at render time, never for arithmetic that feeds an
order. `db/base.py` carries a `Money` type that stores as `NUMERIC(38,18)` on
PostgreSQL and as text on SQLite, because SQLite's NUMERIC affinity silently
converts to float.

### The instrument is a record, not a class hierarchy

There is no `BTCPerpetual` class. `InstrumentSpec` is a ~40-field Pydantic
model, and every calculation is driven by those fields. Adding an instrument is
a YAML entry or an API call — there is a test that constructs an instrument the
code has never seen (a wheat future quoted in EUR, 5000 units per lot) and
hedges it successfully.

The alternative — subclasses per instrument type — puts venue-specific
behaviour in code, which means adding a symbol means a deploy, and means the
behaviour of a symbol is not inspectable at runtime.

### Exposure is the lingua franca

Contracts and lots are not comparable. Base units, notional, quote delta and
account-currency delta are. Every cross-venue calculation goes through
`QuantityConverter.exposure()`, and the four measures are kept separate because
**they genuinely differ** — for an inverse contract, and for legs that quote in
different currencies. Collapsing them to one number would silently pick an
objective on the user's behalf.

### The state machine is data, and write-ahead

Transitions are a declared table, validated on every move; an illegal
transition raises rather than being silently applied. Each transition is
persisted **before** the side effect it describes is attempted, so a process
that dies between "leg 2 submitted" and "leg 2 filled" leaves a record saying
an order was submitted with an unknown outcome. That distinction is the whole
basis of restart recovery — see [EXECUTION_ENGINE.md](EXECUTION_ENGINE.md).

### Failure routing depends on exposure, not on the error

`CycleState.has_exposure` decides whether a failure becomes `FAILED` (nothing
traded, safe to abandon) or `RECOVERY_REQUIRED` (leg 1 is on the book). The
same rejection is a non-event before leg 1 and an incident after it. Routing on
the error type instead would get this backwards half the time.

### A timeout is not a failure

`VenueTimeout` means the outcome is *unknown*. Retrying blindly is how a book
ends up double-hedged. The coordinator queries the venue's open orders for the
client order id it sent, and only if nothing matches does it give up — loudly,
without retrying.

### The venue is authoritative, and adopting its state is explicit

The venue holds the money. When the database and the venue disagree, the venue
is right. But adopting its state overwrites the platform's record, so it is an
explicit, confirmed, audited action rather than an automatic side effect of a
read.

### Risk is evaluated against the post-trade book

An unhedged position is, by definition, a large residual exposure. Gating the
hedge on the *current* state would make the risk engine block the one trade
that removes the risk it is complaining about. The check evaluates the book as
it will be after the hedge, and a trade that strictly reduces residual exposure
proceeds even at an elevated level — unless the kill switch itself has fired.

This was a real bug, found by the acceptance run, not a hypothetical.

### Costs are attributed, and the parts must sum

`PnLBreakdown.check_consistency()` asserts the components sum to the net. A
hedged book's P&L is small relative to its gross legs, so "we lost $563" is
useless without "+$7,122 on the perp, −$7,003 on the CFD, −$252 fees, −$245
spread, −$120 funding, −$90 swap, −$1 FX". The FX slice is split out
explicitly, so a leg's price P&L and the effect of converting it are never
conflated.

### The simulator has a basis

Instruments sharing an underlying are driven by one price process, then offset
by a mean-reverting basis. Without it a perp and a CFD would be numerically
identical and cross-venue basis risk — the irreducible risk of this entire
strategy — would be invisible. Randomness is derived from
`hash(seed, key, step)` rather than a stateful generator, so a failing run
replays exactly and the sequence does not depend on how many other instruments
are subscribed.

## Concurrency

The engine is `asyncio` throughout, matching real venue adapters, which are
network clients. Two consequences are designed for:

* **The database recorder opens a short transaction per call** rather than
  holding one across a cycle. A cycle spans round-trips to two venues; pinning
  a connection for its duration would exhaust the pool, and a failure at the
  last step would lose everything already recorded.
* **WebSocket publishing is fire-and-forget** with a bounded per-client queue.
  A client that cannot keep up loses frames and is logged; it never applies
  backpressure to the trading engine.

## What is deliberately not here

* **No live order path.** See [PAPER_TRADING.md](PAPER_TRADING.md).
* **No automatic trading loop.** The background task advances the simulated
  market and streams prices; it does not trade. Rebalancing is driven from the
  API, the CLI or the scenario runner, so the demo stays deterministic and
  nothing happens that a reader did not ask for.
* **No order book beyond aggregated depth.** The paper engine walks synthetic
  levels to produce realistic slippage; it does not model queue position, which
  would matter for a market-making system and does not for a hedger crossing
  the spread.
* **No distributed coordination.** One process owns the book. Multi-process
  operation would need leader election and a shared lock on each hedge pair;
  that is called out in [PROJECT_STATUS.md](../PROJECT_STATUS.md) rather than
  half-built.
