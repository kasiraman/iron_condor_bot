"""Ad-hoc smoke test for weekend_settle_trades.py's get_fees_for_dates() -- the genuinely
new multi-date fee aggregation logic (0DTE only ever needed a single date). Not part of
the deployed bot."""

from unittest.mock import MagicMock

import weekend_settle_trades as st

trade_client = MagicMock()


def activities_for(date_str):
    if date_str == "2026-08-14":  # entry date: opening fees
        return [
            {"activity_type": "FEE", "net_amount": "-0.25"},
            {"activity_type": "FEE", "net_amount": "-0.25"},
            {"activity_type": "FILL", "symbol": "SPY260817P641", "side": "sell", "qty": "1", "price": "1.50"},
        ]
    if date_str == "2026-08-17":  # expiration date: no extra fees this time (held to expiration, OTM)
        return [
            {"activity_type": "FEE", "net_amount": "-0.03"},
        ]
    return []


trade_client.get.side_effect = lambda path, data: activities_for(data["date"])

fee_total, other_activity, fills, err = st.get_fees_for_dates(
    trade_client, ["2026-08-14", "2026-08-17"], ["SPY260817P641"]
)
assert err is None
assert abs(fee_total - (-0.53)) < 1e-9, f"expected -0.53 total across both dates, got {fee_total}"
assert len(fills) == 1
print(f"fee_total across 2 dates = {fee_total:.2f}  fills={len(fills)}  other_activity={other_activity}")
print("TEST (get_fees_for_dates sums across entry + expiration date) PASSED")

# Same date twice (early-close same day as entry) should not double count -- dedup via set()
fee_total2, _, _, _ = st.get_fees_for_dates(trade_client, ["2026-08-14", "2026-08-14"], ["SPY260817P641"])
assert abs(fee_total2 - (-0.50)) < 1e-9, f"expected -0.50 (single date, deduped), got {fee_total2}"
print(f"fee_total (deduped same date) = {fee_total2:.2f}")
print("TEST (dedup same date) PASSED")

print("\nALL WEEKEND SETTLE SMOKE TESTS PASSED")
