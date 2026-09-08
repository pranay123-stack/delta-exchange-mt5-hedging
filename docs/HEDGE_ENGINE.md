# The hedge engine

## The problem in one line

`1 perpetual contract ≠ 1 MT5 lot`, and the ratio between them is different for
every pair.

From the shipped catalogue, computed by the engine:

```
1 BTCUSDT-PERP contract (1 contract = 0.001 BTC)  = 0.001    BTCUSD  lot (1 lot = 1 BTC)
1 ETHUSDT-PERP contract (1 contract = 0.01 ETH)   = 0.001    ETHUSD  lot (1 lot = 10 ETH)
1 PAXGUSDT-PERP contract (1 contract = 0.01 PAXG) = 0.0001   XAUUSD  lot (1 lot = 100 XAU)
1 SOLUSDT-PERP contract (1 contract = 1 SOL)      = 0.01     SOLUSD  lot (1 lot = 100 SOL)
1 BTCUSD-PERP-INV contract (1 contract = 1 USD)   = 0.0000975 BTCUSDm lot (1 lot = 0.1 BTC)
```

Note BTC and ETH share a ratio of `0.001` while having completely unrelated
contract sizes. Equal ratios do not mean equal economics, which is why the
engine converts through base units on every call rather than caching a ratio.

## The conversion

Everything routes through one projection so heterogeneous instruments become
comparable:

```
position (qty in venue units)
   → base_units        = qty × units_per_quantity          (linear)
                       = qty × contract_size / price       (inverse)
   → notional_quote    = base_units × price                (linear)
                       = qty × contract_size               (inverse, price-independent)
   → quote_delta       = base_units                        (linear)
                       = notional_quote / entry_price      (inverse)
   → account_delta     = quote_delta × FX(quote → account currency)
```

### The inverse-contract trap

An inverse perpetual is denominated in *quote* units. Long `N` quote units
entered at `E`, marked at `S`:

```
PnL_base  = N × (1/E − 1/S)
PnL_quote = PnL_base × S = N × (S/E − 1)
d PnL_quote / d S = N / E          ← depends on the ENTRY price, not the mark
```

while the mark-to-market base holding is `N / S`. So for a $500,000 inverse
position entered at 100,000 and marked at 125,000:

| Objective | Hedge |
|---|---|
| `BASE_ASSET_NEUTRAL` | 4.00 lots — matches the 4 BTC you currently hold |
| `QUOTE_PNL_NEUTRAL` | 5.00 lots — matches the actual price sensitivity |

A 25% difference in hedge size, on the same position, depending on which
question you are asking. Both answers are correct; picking the wrong one leaves
a quarter of the book unhedged. Tested in
`test_inverse_base_neutral_and_delta_neutral_diverge_when_in_profit`.

## The nine objectives

| Objective | Neutralises | Use when |
|---|---|---|
| `BASE_ASSET_NEUTRAL` | base-asset units | you care how much BTC you hold |
| `NOTIONAL_NEUTRAL` | notional in the account currency | the legs track different underlyings |
| `QUOTE_PNL_NEUTRAL` | `dPnL/dS` in quote terms | you care about price sensitivity |
| `ACCOUNT_CCY_PNL_NEUTRAL` | `dPnL/dS` after FX | the legs settle in different currencies |
| `CUSTOM_RATIO` | delta × an explicit ratio | you have a view |
| `PARTIAL` | delta × a ratio below 1 | you want deliberate residual exposure |
| `FUNDING_ADJUSTED` | mean-variance optimum vs carry | carry is expensive |
| `COST_ADJUSTED` | mean-variance optimum vs carry + execution | carry *and* fees are expensive |
| `RISK_WEIGHTED` | beta-adjusted delta | the legs are imperfectly correlated |

They are not interchangeable. On a live 5-BTC position with a USDT-quoted
source and a USD-quoted hedge:

```
BASE_ASSET_NEUTRAL       -5      lots   residual   2.00 bps
NOTIONAL_NEUTRAL         -4.99   lots   residual  18.01 bps
QUOTE_PNL_NEUTRAL        -5      lots   residual   2.00 bps
ACCOUNT_CCY_PNL_NEUTRAL  -4.99   lots   residual  18.01 bps
```

The 2 bps floor on the "exact" objectives is the USDT/USD rate: a hedge that is
perfectly delta-neutral in quote terms is *not* neutral in account terms.
`ACCOUNT_CCY_PNL_NEUTRAL` drives that to zero but leaves a rounding residual
instead, because 4.99005 lots is not on the 0.01 lattice. There is no free
lunch; the engine's job is to show you which residual you are choosing.

### The mean-variance objectives

`FUNDING_ADJUSTED` and `COST_ADJUSTED` minimise carry against the variance of
the hedged position:

```
Var(h) = σs² − 2·h·ρ·σs·σh + h²·σh²

min_h  h·k + (λ/2)·Var(h)     ⟹     h* = β − k / (λ·σh²)      β = ρ·σs/σh
```

The optimum is the minimum-variance ratio **β**, pulled down by a carry term
scaled by the **hedge leg's** variance. This also makes the objectives
consistent with one another: `RISK_WEIGHTED` is exactly the `k = 0` case.

> **A bug worth recording.** An earlier version divided by the *residual*
> variance, `σs²(1−ρ²)`. Against the shipped default parameters that produced
> 0.92 and looked entirely plausible. Once volatility was estimated from real
> prices, correlation measured 0.9998 rather than the assumed 0.995 — making
> the residual variance roughly 300× smaller and the carry penalty 300× too
> large. The objective returned **zero** for a pair whose hedge demonstrably
> removes 98.8% of drawdown. The error was invisible against assumptions and
> obvious against measurements, which is the argument for estimating in one
> sentence.

**`k` is the marginal cost, not the total.** The source position's funding is
paid whether or not it is hedged, so charging it against the hedge would argue
for not hedging a position precisely because its own funding is expensive —
which is backwards. Only the hedge leg's swap or funding enters `k`. Enforced
by `test_adaptive_objectives_ignore_the_source_leg_funding`.

Calibration: with a hedge-leg vol near 3%/day, `λ = 250` means a hedge costing
1 bp/day trims the ratio about 0.4 percentage points below β.

Behaviour under both assumed and measured statistics, from the running system:

| | β | no carry | BTC swap (1.76 bp/day) | 10× carry |
|---|---|---|---|---|
| assumed (ρ=0.995) | 0.995000 | 0.995000 | 0.994218 | 0.987178 |
| measured (ρ=0.99998) | 0.999618 | 0.999618 | 0.998696 | 0.990403 |

The trim is proportional to carry and stable across a 300× change in the
residual-variance estimate — which is the property the earlier formula lacked.

### Where the statistics come from

`marketdata/stats.py` estimates σ and ρ from prices the platform has actually
observed. Three decisions, each a real trade-off:

* **Statistics are computed in float, not `Decimal`.** Everywhere else this
  platform uses `Decimal`, because a quantity that misses a venue's lattice by
  one ULP is a rejected order. A volatility estimate is not that kind of
  number: its sampling error after 200 observations is several percent, around
  fourteen orders of magnitude larger than float precision. The boundary is
  explicit — floats live inside that module, results leave as `Decimal`.

* **Returns are normalised by *observed* elapsed time**, not by the configured
  sampling interval. The interval is a minimum spacing, not a guarantee: when
  the feed ticks more slowly, every observation covers more time than
  configured, and scaling by the configured value overstates volatility by
  `sqrt(actual/configured)`. That was a silent 3.4× error when a five-minute
  setting met an hourly feed. Dividing each return by `sqrt(elapsed)` removes
  the dependence entirely, and handles an irregular feed for free.

* **Sampling is coarse on purpose.** Measured on the simulator, sampling every
  second inflates volatility 12% and — far worse — collapses the measured
  correlation from 0.998 to **0.79**, because the mean-reverting basis
  dominates at short horizons. Since β = ρ·σs/σh, that would corrupt every
  risk-weighted hedge. The default is one sample per five simulated minutes.

Below `min_samples` the estimator refuses to answer and the objectives fall
back to the configured assumptions, with a warning on the calculation saying
so. A number derived from eight observations should not be presented as a
measurement.

Accuracy against the simulator's configured volatilities, across a 6.6× range:

```
instrument                     est daily vol  configured   ratio
PAPER_DELTA:BTCUSDT-PERP             0.02896     0.02879    1.01
PAPER_DELTA:ETHUSDT-PERP             0.03504     0.03559    0.98
PAPER_DELTA:SOLUSDT-PERP             0.04885     0.04816    1.01
PAPER_MT5:XAUUSD                     0.00730     0.00733    1.00
```

and independent of the feed rate across a 24× range of tick intervals.

### Beta comes with its uncertainty

β is the OLS slope of source returns on hedge returns, so it has the usual
standard error:

```
SE(β) = σ_residual / (σ_hedge · √n) = σ_source · √(1−ρ²) / (σ_hedge · √n)
```

Reporting the point estimate alone invites reading sampling error as signal —
and for a hedge ratio, "reading it" means trading it. Measured on the
simulator, whose basis process puts the true β slightly below 1:

| samples | β | s.e. | 2 s.e. band | distinguishable from 1? |
|---|---|---|---|---|
| 199 | 0.99928 | 0.00211 | [0.9951, 1.0035] | no |
| 499 | 0.99857 | 0.00128 | [0.9960, 1.0011] | no |
| 1499 | 0.99885 | 0.00074 | [0.9974, 1.0003] | no |
| 3999 | 0.99893 | 0.00047 | [0.9980, 0.9999] | **yes** |

The estimate is stable from 200 samples onward; what changes is whether the
1‑in‑1000 deviation from parity is *measurable*. Until it is, the dashboard
labels it "not distinct from 1" rather than inviting a trade on noise.

Note also that correlation is not uniform across pairs. Gold measures ρ ≈ 0.94
against ρ ≈ 0.996 for BTC, because gold's daily volatility is roughly four
times lower while the simulator's basis noise is the same absolute size — so
the noise-to-signal ratio is proportionally higher. That is the correct answer,
not a defect, and it is exactly the kind of thing an assumed constant hides.

## The optimiser

The exact solution rarely lands on the venue's lattice. Rounding is one choice;
searching the neighbouring points is another. On a real ETH example
(3737 contracts of 0.01 ETH, hedged with 10-ETH lots on a 0.01 step):

```
  qty      residual    cost    margin    score
 -3.76     63.54 bps   72.57    1447     0.6923
 -3.75     36.78 bps   72.38    1444     0.4556
 -3.74     10.03 bps   72.18    1440     0.2189
 -3.73     16.73 bps   71.99    1436     0.1985   ← weighted default
 -3.72     43.48 bps   71.80    1432     0.3078
```

Naive rounding toward zero gives −3.73 (16.73 bps). `MIN_RESIDUAL` finds −3.74
(10.03 bps) — a 40% reduction in residual exposure for $0.19 more cost. The
weighted default still prefers −3.73 because it also weighs cost and margin;
that is a *choice*, and the candidate table makes it visible rather than
implicit.

Three modes: `EXACT` (round), `STEP_SEARCH` (best on one named priority),
`WEIGHTED` (normalised weighted sum over seven priorities). Scoring normalises
each metric against the observed range in the candidate set, so weights are
comparable across basis points and dollars, and a metric that is identical
across candidates contributes nothing instead of dominating by scale.

## Every calculation shows its work

`HedgeCalculation.steps` is an ordered list of `(label, formula, value)`. The
dashboard renders it, the audit log stores it in the `CALCULATED` transition
payload, and the CLI prints it. A hedge number nobody can reconstruct is not
usable in production.

```
 1. Market data         BTCUSDT-PERP mid / BTCUSD mid    102524.5 USDT / 102521.335 USD
 2. Contract sizing     1 contract = 0.001 BTC | 1 lot = 1 BTC
 3. FX conversion       USDT/USD = 0.9998, USD/USD = 1
 4. Source exposure     5000 × 0.001 BTC                 base=5 BTC, delta=5
 5. Hedge exp. per unit 1 lot of BTCUSD                  base=1 BTC, delta=1
 6. Objective           qty = −source.quote_delta × ratio / hedge_unit.quote_delta
 7. Venue rounding      step=0.01, min=0.01, toward zero  −5 → −5 (error 0)
 8. Residual exposure   source + hedge (signed)           delta=0, ratio=1
 9. Execution cost      fees 0 + half-spread 205.075      205.075 USD
10. Carry (per day)     source funding … ; hedge swap …   −285.716 USD/day
11. Margin              |notional| × 0.01                 5126.07 USD
12. Expected P&L        carry − execution                 −490.791 USD
13. Worst case (99%)    expected − z₉₉·|residual|·σ·√days −497.945 USD
14. Break-even move     execution cost / source notional  0.040013 %
```

## Rounding policy

Quantities round **toward zero** by default. Under-hedging leaves a known
residual the rebalancer can close; over-hedging creates exposure in the
opposite direction that nobody asked for. A quantity below the venue minimum
becomes zero and is flagged — never silently promoted up to the minimum, which
would execute more than the calculation asked for.
