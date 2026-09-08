# Demo script

Two options: the automated 25-step run, or a guided tour of the dashboard.

## Option 1 — the automated acceptance run

```bash
docker compose exec backend hedgelab demo
# or locally:  cd backend && .venv/bin/hedgelab demo
```

Twenty-five steps, each asserting an observable outcome, exiting non-zero if
any fails. Takes about ten seconds. Currently **25/25**.

Worth pausing on:

* **Step 6** prints the full 14-step derivation of the hedge quantity.
* **Step 7** shows the unhedged pair sitting at `EMERGENCY` with 10,000 bps of
  residual — and step 8 hedges it anyway, because risk is evaluated against the
  post-trade book.
* **Step 13** shows a 4% move producing almost no P&L change on a hedged book.
* **Step 15** states plainly that the hedge *loses money* at the raised funding
  rate.
* **Steps 20–22** wipe the in-memory venues, detect the gap, rehydrate and
  confirm `resumable=True`.
* **Step 24** flattens everything and then verifies it actually reached flat.

## Option 2 — the guided dashboard tour (about 10 minutes)

`docker compose up --build`, then <http://localhost:5174>.

### 1. Overview (30s)

Two venues, both connected, both paper. Note the permanent **PAPER TRADING
ONLY** banner and `0 live adapters registered` in the header — that is not a
label, it is the actual registry count.

### 2. Configuration → the instrument catalogue (2 min)

Ten instruments across two venues. Look at the **Sizing** column:

```
BTCUSDT-PERP    1 contract = 0.001 BTC
BTCUSD          1 lot      = 1 BTC
ETHUSDT-PERP    1 contract = 0.01 ETH
ETHUSD          1 lot      = 10 ETH
XAUUSD          1 lot      = 100 XAU
BTCUSD-PERP-INV 1 contract = 1 USD (inverse)
```

Six different definitions of "one". This is the problem the platform exists to
solve.

Scroll to **Add an instrument** and paste the placeholder silver CFD. It
appears in the catalogue and starts quoting immediately — no deploy, no code
change. The change log at the bottom records it.

### 3. Hedge Calculator (3 min)

Source `PAPER_DELTA:BTCUSDT-PERP`, hedge `PAPER_MT5:BTCUSD`, quantity `5000`.

Run it once with **`QUOTE_PNL_NEUTRAL`**: 5000 contracts → −5 lots, and the
derivation table shows every step including the FX conversion.

Now switch to **`ACCOUNT_CCY_PNL_NEUTRAL`**: −4.99 lots. The difference is the
USDT/USD rate. A hedge that is perfectly delta-neutral in quote terms is not
neutral in account terms, and the platform makes you choose which one you meant.

Then the interesting one. Source `PAPER_DELTA:BTCUSD-PERP-INV`, quantity
`500000`, and compare `BASE_ASSET_NEUTRAL` against `QUOTE_PNL_NEUTRAL`. The
answers differ by 25% on the same position, because an inverse contract's delta
depends on its **entry** price, not its mark.

Finally tick **Search the quantity lattice**, set source `ETHUSDT-PERP`,
quantity `3737`, objective `BASE_ASSET_NEUTRAL`, mode `STEP_SEARCH`, priority
`MIN_RESIDUAL`. The candidate table shows rounding gives 16.73 bps of residual
while the neighbouring lattice point gives 10.03 bps — for $0.19 more cost.

### 4. Execute a hedge (1 min)

Hedge Pairs → **BTC perp → BTC CFD** → *Hedge existing position*. Then Hedge
Cycles: the cycle walked nine states, and each one has its payload, including
the full calculation stored on the `CALCULATED` transition.

### 5. Break it (3 min)

Fault Injection is the page worth spending time on.

* **Simulate broker disconnect** → try to hedge → the cycle fails *before*
  taking exposure, and says so.
* **Reconnect**, then **Arm leg-2 partial fill** → hedge → the cycle goes
  `LEG_2_PARTIAL → REBALANCING → COMPLETED` and the shortfall is traded away.
* **Arm order rejection** → hedge → `RECOVERY_REQUIRED → REBALANCING →
  COMPLETED`. Leg 1 was on the book and the system unwound the imbalance.
* **Create an unexpected venue position**, then look at the reconciliation
  panel: `UNKNOWN_POSITION`, critical, with a suggested action.
* **Run scenario J** (restart recovery) and read the steps: state discarded,
  gap detected, rehydrated, `resumable=True`.
* **Engage the kill switch** → every position flattened, further trading
  refused with a 409.

### 6. Risk Dashboard (1 min)

Margin utilisation, liquidation distance per leg, exposure by venue,
underlying and settlement currency, and the recorded risk events with the
threshold each one breached.

## Option 3 — CLI only

```bash
hedgelab instruments pairs        # the conversions
hedgelab calculate --source PAPER_DELTA:BTCUSD-PERP-INV \
                   --hedge PAPER_MT5:BTCUSDm \
                   --quantity 500000 --objective QUOTE_PNL_NEUTRAL
hedgelab optimize --source PAPER_DELTA:ETHUSDT-PERP --hedge PAPER_MT5:ETHUSD \
                  --quantity 3737 --mode STEP_SEARCH --priority MIN_RESIDUAL
hedgelab scenario run-all
hedgelab backtest run --steps 500 --step-seconds 3600
```

## The three points worth making

1. **`1 contract ≠ 1 lot`, and the ratio differs per pair.** Quantity matching
   is a 1000× position error on the BTC pair, not a rounding issue.
2. **The objective you pick changes the answer**, materially, for inverse
   contracts and for legs settling in different currencies. The platform shows
   the difference instead of choosing for you.
3. **The second leg is where hedging actually goes wrong**, and every failure
   mode has a specific, demonstrable, tested response — including the ones that
   only appear after a restart.
