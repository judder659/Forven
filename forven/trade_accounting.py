"""Consistent dollar accounting for legacy live and kernel paper records."""


def net_pnl_sql() -> str:
    """SQL expression over a trades row, using recorded costs without rewriting it.

    Kernel paper dollars already include fees, slippage and funding. Legacy/live
    net percentages are margin returns; their dollar field remains gross.
    Unmeasured costs remain unknown rather than being estimated from current settings.
    """
    sd = "CASE WHEN json_valid(signal_data) THEN signal_data ELSE '{}' END"
    gross = "COALESCE(pnl_usd, pnl)"
    margin = (
        "ABS(COALESCE(fill_entry_price, entry_price, signal_entry_price, 0) * COALESCE(size, 0))"
        " / CASE WHEN COALESCE(leverage, 0) > 0 THEN leverage ELSE 1.0 END"
    )
    return f"""CASE
        WHEN json_extract({sd}, '$.gross_pnl_usd') IS NOT NULL
          OR json_extract({sd}, '$.pnl_is_equity_fraction') = 1 THEN {gross}
        WHEN net_pnl_pct IS NOT NULL AND ({margin}) > 0 THEN net_pnl_pct * ({margin})
        ELSE {gross} - COALESCE(fees_pct, 0) * ({margin})
          - COALESCE(json_extract({sd}, '$.funding_usd'), json_extract({sd}, '$.close_funding_usd'), 0)
        END"""
