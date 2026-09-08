"""Quantity conversion: the `1 contract != 1 lot` problem.

Table-driven across many contract-size x lot-size x step combinations,
including the ones that trip naive implementations: non-decimal steps,
minimums that are not one step, inverse contracts, and instruments whose
quantity unit implies a different number of base units on each venue.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal

import pytest
from tests.conftest import make_cfd, make_perp

from hedgelab.domain.quantity import ConversionError, QuantityConverter, describe_conversion

D = Decimal


# ----------------------------------------------------------------------
# base-unit conversion across venue quantity units
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("contract_size", "units_per_lot", "contracts", "expected_base", "expected_lots"),
    [
        # The three combinations named in the brief.
        ("1", "1", "10", "10", "10"),
        ("0.001", "100", "50000", "50", "0.5"),
        ("10", "0.1", "3", "30", "300"),
        # Realistic pairings.
        ("0.001", "1", "5000", "5", "5"),          # BTC perp -> BTC CFD
        ("0.01", "10", "4000", "40", "4"),         # ETH perp -> ETH CFD
        ("0.01", "100", "20000", "200", "2"),      # gold perp -> XAUUSD
        ("1", "100", "700", "700", "7"),           # SOL perp -> SOL CFD
        ("0.001", "0.1", "1234", "1.234", "12.34"),
        # Awkward ratios that do not divide evenly.
        ("0.003", "7", "1000", "3", "0.428571428571428571428571429"),
    ],
)
def test_contracts_convert_to_lots_through_base_units(
    contract_size: str, units_per_lot: str, contracts: str,
    expected_base: str, expected_lots: str,
) -> None:
    perp = make_perp(contract_size=contract_size)
    cfd = make_cfd(units_per_lot=units_per_lot)
    price = D("50000")

    base = QuantityConverter(perp).base_units(D(contracts), price)
    assert base == D(expected_base)

    lots = QuantityConverter(perp).convert_to(cfd, D(contracts), price, price)
    # Decimal division can produce a repeating expansion; compare on the
    # significant digits rather than demanding bit-identical representations.
    assert abs(lots - D(expected_lots)) < D("1e-24")


def test_conversion_is_reversible() -> None:
    """Converting there and back must return the original quantity."""
    perp = make_perp(contract_size="0.001")
    cfd = make_cfd(units_per_lot="10")
    price = D("3850.25")

    lots = QuantityConverter(perp).convert_to(cfd, D("7777"), price, price)
    back = QuantityConverter(cfd).convert_to(perp, lots, price, price)
    assert back == D("7777")


def test_quantity_matching_is_not_the_same_as_exposure_matching() -> None:
    """The headline failure mode: matching the *number* leaves 99.9% unhedged."""
    perp = make_perp(contract_size="0.001")     # 1 contract = 0.001 BTC
    cfd = make_cfd(units_per_lot="1")           # 1 lot     = 1 BTC
    price = D("100000")

    naive_hedge_lots = D("1000")   # "1000 contracts, so 1000 lots"
    correct_hedge_lots = QuantityConverter(perp).convert_to(cfd, D("1000"), price, price)

    assert correct_hedge_lots == D("1")
    assert naive_hedge_lots == correct_hedge_lots * 1000

    source_base = QuantityConverter(perp).base_units(D("1000"), price)
    naive_base = QuantityConverter(cfd).base_units(naive_hedge_lots, price)
    assert source_base == D("1")
    assert naive_base == D("1000")  # a 1000x over-hedge


def test_conversion_between_different_underlyings_is_refused() -> None:
    btc = make_perp(base="BTC", underlying="BTC")
    gold = make_cfd(base="XAU", underlying="XAU")
    with pytest.raises(ConversionError, match="different underlyings"):
        QuantityConverter(btc).convert_to(gold, D("1"), D("100000"), D("2400"))


def test_conversion_uses_underlying_key_not_base_asset() -> None:
    """PAXG and XAU are spelled differently but track the same thing."""
    paxg = make_perp(base="PAXG", underlying="XAU", contract_size="0.01")
    xauusd = make_cfd(base="XAU", underlying="XAU", units_per_lot="100")
    lots = QuantityConverter(paxg).convert_to(xauusd, D("20000"), D("2400"), D("2400"))
    assert lots == D("2")  # 20000 x 0.01 oz = 200 oz = 2 lots of 100 oz


# ----------------------------------------------------------------------
# inverse contracts
# ----------------------------------------------------------------------
def test_inverse_notional_is_price_independent() -> None:
    """An inverse contract is defined in quote units, so notional never moves."""
    inv = make_perp(inverse=True, contract_size="1", quote="USD", base="BTC")
    converter = QuantityConverter(inv)
    for price in ("50000", "100000", "150000"):
        assert converter.notional_quote(D("100000"), D(price)) == D("100000")


def test_inverse_base_units_move_with_price() -> None:
    inv = make_perp(inverse=True, contract_size="1")
    converter = QuantityConverter(inv)
    assert converter.base_units(D("100000"), D("50000")) == D("2")
    assert converter.base_units(D("100000"), D("100000")) == D("1")


def test_inverse_delta_depends_on_entry_not_mark() -> None:
    """dPnL/dS = N/entry for an inverse contract.  This is the trap."""
    inv = make_perp(inverse=True, contract_size="1")
    converter = QuantityConverter(inv)
    notional = D("100000")

    delta_at_entry = converter.quote_delta(notional, D("100000"), entry_price=D("100000"))
    delta_after_move = converter.quote_delta(notional, D("125000"), entry_price=D("100000"))
    # The mark moved 25% but the delta did not change at all.
    assert delta_at_entry == D("1")
    assert delta_after_move == D("1")

    # Whereas the mark-to-market base holding *did* fall.
    assert converter.base_units(notional, D("125000")) == D("0.8")


def test_inverse_falls_back_to_mark_when_no_entry_is_known() -> None:
    inv = make_perp(inverse=True, contract_size="1")
    converter = QuantityConverter(inv)
    assert converter.quote_delta(D("100000"), D("80000")) == D("1.25")


def test_inverse_conversion_requires_a_positive_price() -> None:
    inv = make_perp(inverse=True)
    with pytest.raises(ConversionError, match="positive price"):
        QuantityConverter(inv).base_units(D("100"), D("0"))


def test_linear_delta_equals_base_units() -> None:
    """For linear instruments the two measures coincide -- by construction."""
    perp = make_perp(contract_size="0.5")
    converter = QuantityConverter(perp)
    for price in ("10", "1000", "99999"):
        qty = D("37")
        assert converter.base_units(qty, D(price)) == converter.quote_delta(qty, D(price))


# ----------------------------------------------------------------------
# lattice rounding
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("step", "minimum", "raw", "expected", "below_min"),
    [
        ("0.01", "0.01", "1.237", "1.23", False),
        ("0.01", "0.01", "-1.237", "-1.23", False),   # toward zero, both signs
        ("0.1", "0.1", "0.05", "0", True),
        ("1", "1", "0.9", "0", True),
        ("1", "1", "17.99", "17", False),
        ("0.25", "0.25", "1.6", "1.5", False),
        ("0.05", "0.1", "0.14", "0.1", False),
        ("100", "100", "1250", "1200", False),
    ],
)
def test_round_quantity_snaps_toward_zero(
    step: str, minimum: str, raw: str, expected: str, below_min: bool
) -> None:
    spec = make_cfd(quantity_step=step, min_quantity=minimum)
    result = QuantityConverter(spec).round_quantity(D(raw))
    assert result.rounded == D(expected)
    assert result.below_minimum is below_min
    assert result.is_executable is (D(expected) != 0)


def test_rounding_never_promotes_up_to_the_minimum() -> None:
    """Rounding up to reach the minimum would execute more than was asked for."""
    spec = make_cfd(quantity_step="0.01", min_quantity="1")
    result = QuantityConverter(spec).round_quantity(D("0.4"))
    assert result.rounded == D("0")
    assert result.below_minimum is True


def test_rounding_caps_at_the_venue_maximum() -> None:
    spec = make_cfd(quantity_step="0.01", min_quantity="0.01")
    huge = spec.max_quantity * 10
    result = QuantityConverter(spec).round_quantity(huge)
    assert result.above_maximum is True
    assert result.rounded == spec.max_quantity


def test_rounding_error_is_reported_and_signed() -> None:
    spec = make_cfd(quantity_step="0.1", min_quantity="0.1")
    result = QuantityConverter(spec).round_quantity(D("2.37"))
    assert result.rounded == D("2.3")
    assert result.rounding_error == D("2.3") - D("2.37")
    assert result.rounding_error < 0


@pytest.mark.parametrize("mode", [ROUND_DOWN, ROUND_HALF_UP, ROUND_CEILING])
def test_rounded_quantity_is_always_on_the_lattice(mode: str) -> None:
    spec = make_cfd(quantity_step="0.03", min_quantity="0.03")
    for raw in ("0.1", "1.7", "22.222", "0.031"):
        rounded = QuantityConverter(spec).round_quantity(D(raw), mode=mode).rounded
        assert (rounded / spec.quantity_step) % 1 == 0


# ----------------------------------------------------------------------
# price rounding
# ----------------------------------------------------------------------
def test_round_price_snaps_to_tick() -> None:
    spec = make_cfd(tick_size="0.5")
    converter = QuantityConverter(spec)
    assert converter.round_price(D("100.24")) == D("100")
    assert converter.round_price(D("100.26")) == D("100.5")


def test_conservative_price_rounding_never_crosses_the_spread() -> None:
    """A buy limit rounds down, a sell limit rounds up.  Never the other way."""
    spec = make_cfd(tick_size="0.5")
    converter = QuantityConverter(spec)
    assert converter.round_price_conservative(D("100.9"), is_buy=True) == D("100.5")
    assert converter.round_price_conservative(D("100.1"), is_buy=False) == D("100.5")


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------
def test_describe_conversion_is_human_readable() -> None:
    perp = make_perp(symbol="X-PERP", contract_size="0.001")
    cfd = make_cfd(symbol="XUSD", units_per_lot="10")
    text = describe_conversion(perp, cfd, D("1000"), D("1000"))
    assert "X-PERP" in text and "XUSD" in text
    assert "0.0001" in text


def test_describe_conversion_reports_incompatibility_instead_of_raising() -> None:
    text = describe_conversion(
        make_perp(base="BTC", underlying="BTC"),
        make_cfd(base="XAU", underlying="XAU"),
        D("1"), D("1"),
    )
    assert "no base-unit conversion" in text
