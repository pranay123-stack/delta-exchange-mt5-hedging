# The risk engine

Two levels, because they answer different questions. A pair can be inside its
own limits while the portfolio is not: five pairs each at 90% of their
individual limit is a portfolio at 450% of what any one of them was allowed.

## The threshold ladder

`NORMAL → WARNING → DANGER → EMERGENCY → KILL_SWITCH`. The **worst** breaching
metric sets the level for the whole assessment. From `DANGER` upward,
`is_tradable` is false and new hedges are blocked.

Actions escalate cumulatively:

| Level | Actions |
|---|---|
| `WARNING` | `REBALANCE` (if the breach is residual exposure) |
| `DANGER` | + `STOP_NEW_TRADES`, `PAUSE_PAIR` |
| `EMERGENCY` | + `CANCEL_OPEN_ORDERS`, `REDUCE_EXPOSURE`, `ENTER_EMERGENCY_MODE` |
| `KILL_SWITCH` | + `FLATTEN_POSITIONS` |

Every breach becomes a `risk_events` row; every action taken becomes an
`emergency_actions` row with its result. A risk system whose decisions cannot
be reconstructed after the fact is not auditable.

## What is checked, per pair

| Metric | Thresholds (default) |
|---|---|
| Margin level, per venue | 200 / 150 / 120 / 100 % |
| Residual exposure | 50 / 150 / 400 bps of source notional |
| Distance to liquidation | 10% / 5% / 2% |
| Daily loss | 60% / 85% / 100% of the limit |
| Stale market data | `DANGER` immediately |
| Spread | `WARNING` past the execution limit |
| Cross-venue basis | 30 / 80 bps |

Basis deserves its own line: it is the irreducible risk of a cross-venue hedge.
The two legs can move apart even when the underlying does not move at all, and
no hedge ratio removes it.

## Margin and liquidation

Closed-form throughout — no search loops — so the numbers are exact and
testable against hand calculations.

```
equity       = balance + unrealised PnL
free_margin  = equity − used_margin
margin_level = equity / used_margin × 100
```

The broker's stop-out and the exchange's liquidation threshold are both floors
on `margin_level`, so one model covers both.

### Linear liquidation

```
long  (N units):   S_liq = (E·N − eq₀) / (N · (1 − mmr))
short (M units):   S_liq = (eq₀ + E·M) / (M · (1 + mmr))
```

Verified against the closed form in tests: BTCUSD at 100,000 with 1% initial
and 0.5% maintenance liquidates a long at 99,497.49 — exactly
`E·(1−imr)/(1−mmr)`.

### Inverse liquidation

An inverse contract's P&L is affine in `1/S`, not in `S`, so it needs its own
formula:

```
long  (N quote units):  S_liq = N · (1 + mmr) / (eq₀ + N/E)
short (M quote units):  S_liq = M · (1 − mmr) / (M/E − eq₀)
```

The consequence is **asymmetry**: a $100,000 inverse position at 2% margin
survives a 1.47% drop when long but a 1.53% rise when short. That convexity is
real, and a linear approximation would put the liquidation price in the wrong
place on both sides.

### Fully funded positions

When equity exceeds the notional there is no adverse move that liquidates the
position — only the price going through zero. The engine returns
`liquidation_price = None` with a note, rather than a nonsensical negative
price.

## Portfolio risk

Aggregates that only exist at the portfolio level:

* **Gross notional** — both legs counted, so a fully hedged pair contributes
  twice its position size. The limit is calibrated accordingly.
* **Net exposure** — the sum of *signed* residuals, not of magnitudes. Two
  pairs residual-long and residual-short genuinely offset.
* **Margin utilisation** across venues.
* **Concentration** by underlying.
* **Currency exposure** by settlement asset — two legs settling in different
  currencies leave FX exposure even when price exposure is flat.
* **Worst-case loss** — every residual moving adversely by the tightest
  liquidation distance at once. A deliberately blunt stress, because the limit
  exists to stop a correlated blow-up.

### Concentration needs its own grading

The generic 75/90/100% ramp is wrong for a share-of-a-whole metric, for two
reasons:

1. A portfolio with `n` underlyings has a **floor** of `100/n` percent
   concentration. Warning at 75% of a 60% limit (45%) would fire on every
   two-asset book no matter how evenly balanced.
2. A single open pair is trivially 100% concentrated, which says nothing.

So concentration is skipped below two underlyings, breaches only at or above
the limit, and escalates to `DANGER` only past the midpoint between the limit
and total concentration. It never reaches `EMERGENCY`: it describes portfolio
shape, not solvency.

Both behaviours were calibration bugs caught by tests, not hypotheticals.

## Risk is evaluated against the post-trade book

An unhedged position *is* a large residual exposure. Gating the hedge on the
current state makes the risk engine block the trade that fixes the problem it
is reporting. So:

* the check evaluates the book **as it will be** after the hedge;
* account snapshots stay current, because margin level describes money that
  exists now, and the *additional* margin the trade needs is checked separately
  against `max_safe_quantity`;
* a trade that strictly reduces residual exposure proceeds even at an elevated
  level — unless the kill switch itself has fired.

Both halves of this were bugs found by the 25-step acceptance run.

## The kill switch

`POST /api/emergency/kill-switch` requires the `ADMIN` role and an explicit
`X-Confirm-Action: CONFIRM` header. It:

1. sets `trading_paused` and `emergency_mode`, and blocks every new cycle;
2. cancels every working order;
3. flattens every position, **hedge leg first**, retrying partial fills, and
   continuing past any venue it cannot reach;
4. verifies it actually reached flat, and reports the remaining positions and
   any unreachable venue by name if it did not;
5. writes four `emergency_actions` rows and an `audit_logs` entry.

Clearing it is a separate, audited call. It is deliberately not reversible from
inside the same request.

## Configuration

Thresholds and portfolio limits are editable at runtime through
`PATCH /api/risk/thresholds` and `PATCH /api/portfolio/limits`, and every change
is written to `configuration_changes` with its before and after state. The
dashboard's Configuration page edits them directly.
