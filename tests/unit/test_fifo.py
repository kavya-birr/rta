from decimal import Decimal

from openreversefeed.core.fifo import compute_fifo


def _buy(d, units, price):
    return {"action": "buy", "transaction_date": d, "units": units, "unit_price": price}


def _sell(d, units, price):
    return {"action": "sell", "transaction_date": d, "units": units, "unit_price": price}


def test_purchase_only():
    txns = [_buy("2025-01-01", "100", "10"), _buy("2025-02-01", "50", "12")]
    result = compute_fifo(txns)
    assert result.units == Decimal("150")
    assert result.total_cost == Decimal("1600")
    assert len(result.lots) == 2


def test_sell_from_oldest_lot_first():
    txns = [
        _buy("2025-01-01", "100", "10"),
        _buy("2025-02-01", "50", "12"),
        _sell("2025-03-01", "30", "15"),
    ]
    result = compute_fifo(txns)
    assert result.units == Decimal("120")
    assert result.total_cost == Decimal("70") * Decimal("10") + Decimal("50") * Decimal("12")


def test_partial_lot_sale():
    txns = [_buy("2025-01-01", "100", "10"), _sell("2025-02-01", "30", "15")]
    result = compute_fifo(txns)
    assert result.units == Decimal("70")
    assert result.total_cost == Decimal("70") * Decimal("10")


def test_full_lot_then_next_lot():
    txns = [
        _buy("2025-01-01", "10", "5"),
        _buy("2025-02-01", "20", "8"),
        _sell("2025-03-01", "15", "10"),
    ]
    result = compute_fifo(txns)
    # 10 from first lot + 5 from second lot consumed; 15 remaining in second lot
    assert result.units == Decimal("15")
    assert result.total_cost == Decimal("15") * Decimal("8")


def test_overselling_caps_at_zero():
    txns = [_buy("2025-01-01", "10", "10"), _sell("2025-02-01", "100", "15")]
    result = compute_fifo(txns)
    assert result.units == Decimal("0")
    assert result.total_cost == Decimal("0")


def test_empty_input():
    result = compute_fifo([])
    assert result.units == Decimal("0")
    assert result.total_cost == Decimal("0")
    assert result.lots == []


def test_cost_per_unit_zero_safe():
    result = compute_fifo([])
    assert result.cost_per_unit == Decimal("0")
