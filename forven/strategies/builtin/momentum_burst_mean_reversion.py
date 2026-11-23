"""Momentum Burst Mean Reversion -- H01624 / ETH/USDT 4h

Entry trigger: ATR-normalized bar return percentile rank.
Captures momentum overshoot reversals (liquidity sweeps) that RSI systems miss.
ADX regime filter adds directional intelligence.
Long/Short.
"""


import pandas as pd

from forven.strategies.base import BaseStrategy, Signal

TYPE_NAME = "momentum_burst_mean_reversion"


class MomentumBurstMeanReversion(BaseStrategy):

    @property
    def name(self) -> str:
        return f"Momentum Burst MR ({self.asset})"

    @property
    def asset(self) -> str:
        return self.params.get("_asset", "ETH")

    @property
    def strategy_type(self) -> str:
        return TYPE_NAME

    @property
    def default_params(self) -> dict:
        return {
            "atr_period": 14,
            "burst_percentile": 95,
            "burst_threshold": 1.5,
            "adx_period": 14,
            "adx_regime_max": 25,
            "ema_trend": 50,
            "vol_confirm_mult": 1.2,
            "vol_ema_period": 20,
            "atr_stop_mult": 2.0,
            "max_hold_bars": 8,
        }

    @property
    def compatible_regimes(self) -> set[str]:
        return {"RANGE_BOUND", "TREND_UP", "TREND_DOWN"}

    def describe(self) -> str:
        p = self.params
        return (
            f"Momentum Burst MR: enters when ATR-normalized bar return "
            f"exceeds {p['burst_percentile']}th percentile over 20 bars, "
            f"with ADX < {p['adx_regime_max']}, EMA({p['ema_trend']}) alignment, "
            f"and volume > {p['vol_confirm_mult']}x EMA."
        )

    def _atr(self, df, period):
        high = df["high"]
        low = df["low"]
        close = df["close"]
        tr1 = high - low
        tr2 = (high - close.shift(1)).abs()
        tr3 = (low - close.shift(1)).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def _adx(self, df, period, atr):
        high = df["high"]
        low = df["low"]
        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.clip(lower=0)
        minus_dm = minus_dm.clip(lower=0)
        plus_dm[plus_dm < minus_dm] = 0
        minus_dm[minus_dm < plus_dm] = 0
        s_plus = plus_dm.rolling(period).mean()
        s_minus = minus_dm.rolling(period).mean()
        di_plus = (s_plus / atr) * 100
        di_minus = (s_minus / atr) * 100
        dx = (di_plus - di_minus).abs() / (di_plus + di_minus) * 100
        return dx.rolling(period).mean()

    def generate_signal(self, df):
        p = self.params
        close = df["close"]

        min_len = max(
            p["atr_period"], p["adx_period"], p["ema_trend"], p["vol_ema_period"], 21
        ) + 2
        if len(df) < min_len:
            return Signal(
                entry_signal=False, exit_signal=False,
                price=float(close.iloc[-1]), direction="long", confidence=0.0,
                indicators={},
            )

        atr = self._atr(df, int(p["atr_period"]))
        bar_return = close.pct_change()
        atr_normalized = bar_return / atr

        def pct_rank_20(x):
            if len(x) < 20:
                return 0.5
            return pd.Series(x).rank(pct=True).iloc[-1]

        percentile_rank = atr_normalized.rolling(20).apply(pct_rank_20, raw=True)
        adx = self._adx(df, int(p["adx_period"]), atr)
        ema = close.ewm(span=int(p["ema_trend"]), adjust=False).mean()
        vol = df["volume"]
        vol_ema = vol.ewm(span=int(p["vol_ema_period"]), adjust=False).mean()
        vol_confirm = vol > vol_ema * float(p["vol_confirm_mult"])

        curr_pr = float(percentile_rank.iloc[-1])
        curr_atr_norm = float(atr_normalized.iloc[-1])
        curr_adx = float(adx.iloc[-1])
        curr_ema = float(ema.iloc[-1])
        curr_close = float(close.iloc[-1])
        curr_vol_confirm = bool(vol_confirm.iloc[-1])

        long_entry = (
            curr_pr > float(p["burst_percentile"]) / 100
            and curr_atr_norm > float(p["burst_threshold"])
            and curr_adx < float(p["adx_regime_max"])
            and curr_close > curr_ema
            and curr_vol_confirm
        )
        short_entry = (
            curr_pr < (100 - float(p["burst_percentile"])) / 100
            and curr_atr_norm < -float(p["burst_threshold"])
            and curr_adx < float(p["adx_regime_max"])
            and curr_close < curr_ema
            and curr_vol_confirm
        )

        direction = "long" if long_entry else ("short" if short_entry else "long")

        return Signal(
            entry_signal=bool(long_entry or short_entry),
            exit_signal=False,
            price=round(curr_close, 4),
            direction=direction,
            confidence=round(abs(curr_atr_norm) / (abs(curr_atr_norm) + 1.0), 4),
            indicators={
                "pr": round(curr_pr, 4),
                "atr_norm": round(curr_atr_norm, 4),
                "adx": round(curr_adx, 2),
                "ema_50": round(curr_ema, 4),
                "vol_confirm": curr_vol_confirm,
            },
        )

    def parameter_space(self) -> dict:
        return {
            "burst_percentile": (90, 98, 2),
            "burst_threshold": (1.0, 3.0, 0.5),
            "adx_regime_max": (20, 35, 5),
            "vol_confirm_mult": (1.0, 2.0, 0.2),
        }


STRATEGY_CLASS = MomentumBurstMeanReversion

STRATEGIES = [
    ("H01624-ETH-MOM-BURST-4H", MomentumBurstMeanReversion, {"_asset": "ETH"}),
]
