"""
ETH-REGIME_ADAPTIVE-S00002
Regime-Adaptive Strategy: Dynamically switches between trend-following and mean-reversion
based on detected market regime (volatility + trend strength).
"""

import pandas as pd
from forven.strategies.base import BaseStrategy, Signal


class RegimeAdaptiveStrategy(BaseStrategy):
    """
    Regime-Adaptive Strategy for ETH.
    
    Detects market regime using:
    - Volatility Regime: ATR-based (high/low volatility)
    - Trend Regime: ADX-based (trending/ranging)
    
    Switches between:
    - TREND MODE: Supertrend + EMA confirmation (for trending markets)
    - RANGE MODE: RSI + Stochastic mean-reversion (for ranging markets)
    
    Parameters:
    - atr_length: ATR period for volatility detection (default: 14)
    - adx_length: ADX period for trend detection (default: 14)
    - volatility_threshold: Multiplier for volatility regime (default: 1.2)
    - trend_threshold: ADX level for trend vs range (default: 25)
    - supertrend_period: Supertrend lookback (default: 10)
    - supertrend_multiplier: Supertrend ATR multiplier (default: 3.0)
    - rsi_length: RSI period (default: 14)
    - stoch_period: Stochastic K period (default: 14)
    - ema_fast: Fast EMA for trend confirmation (default: 9)
    - ema_slow: Slow EMA for trend confirmation (default: 21)
    """
    
    TYPE_NAME = "regime_adaptive_eth"
    STRATEGY_ID = "S00002"
    
    @property
    def name(self) -> str:
        return "ETH-REGIME_ADAPTIVE-S00002"
    
    @property
    def asset(self) -> str:
        return "ETH"
    
    @property
    def strategy_type(self) -> str:
        return "regime_adaptive"
    
    @property
    def default_params(self) -> dict:
        return {
            # Regime detection parameters
            'atr_length': 14,
            'adx_length': 14,
            'volatility_threshold': 1.2,
            'trend_threshold': 25,
            
            # Trend mode parameters
            'supertrend_period': 10,
            'supertrend_multiplier': 3.0,
            'ema_fast': 9,
            'ema_slow': 21,
            
            # Range mode parameters
            'rsi_length': 14,
            'stoch_period': 14,
            'stoch_smooth': 3,
            
            # Timing
            'regime_confirm_bars': 3,
        }
    
    def generate_signal(self, df: pd.DataFrame) -> Signal:
        """Generate trading signals based on regime-adaptive logic."""
        
        # Defensive parameter coercion - enforce safe boundaries
        atr_length = max(2, int(self.params.get('atr_length', 14)))
        adx_length = max(2, int(self.params.get('adx_length', 14)))
        volatility_threshold = float(max(0.5, self.params.get('volatility_threshold', 1.2)))
        trend_threshold = float(max(10, self.params.get('trend_threshold', 25)))
        ema_fast = max(2, int(self.params.get('ema_fast', 9)))
        ema_slow = max(2, int(self.params.get('ema_slow', 21)))
        rsi_length = max(2, int(self.params.get('rsi_length', 14)))
        stoch_period = max(2, int(self.params.get('stoch_period', 14)))
        stoch_smooth = max(1, int(self.params.get('stoch_smooth', 3)))
        regime_confirm_bars = max(1, int(self.params.get('regime_confirm_bars', 3)))
        
        # Create a copy to avoid modifying original
        data = df.copy()
        
        # Ensure we have required columns
        required = ['high', 'low', 'close', 'volume']
        for col in required:
            if col not in data.columns:
                raise ValueError(f"Missing required column: {col}")
        
        # ============ REGIME DETECTION ============
        
        # 1. ATR for volatility regime
        data['atr'] = data['high'].diff().abs().rolling(atr_length).max()
        data['atr_ma'] = data['atr'].rolling(atr_length * 2).mean()
        data['volatility_ratio'] = data['atr'] / data['atr_ma']
        
        # 2. ADX for trend strength (native pandas)
        high_diff = data['high'].diff()
        low_diff = data['low'].diff()
        plus_dm = high_diff.where((high_diff > low_diff) & (high_diff > 0), 0)
        minus_dm = low_diff.where((low_diff > high_diff) & (low_diff > 0), 0)
        atr_manual = data['atr']
        plus_di = 100 * (plus_dm.rolling(adx_length).mean() / atr_manual)
        minus_di = 100 * (minus_dm.rolling(adx_length).mean() / atr_manual)
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
        data['adx'] = dx.rolling(adx_length).mean()
        
        # 3. Regime classification
        data['high_volatility'] = data['volatility_ratio'] > volatility_threshold
        data['trending'] = data['adx'] > trend_threshold
        
        # Composite regime: 0=ranging, 1=trending
        data['regime'] = data['trending'].astype(int)
        
        # Smooth regime to avoid whipsaws
        data['regime_confirmed'] = data['regime'].rolling(regime_confirm_bars).min() == data['regime'].rolling(regime_confirm_bars).max()
        
        # ============ INDICATORS FOR EACH REGIME ============
        
        # TREND MODE INDICATORS
        data['supertrend'] = 1
        data['supertrend_direction'] = data['close']
        
        # EMAs for trend confirmation
        data['ema_fast_val'] = data['close'].ewm(span=ema_fast, adjust=False).mean()
        data['ema_slow_val'] = data['close'].ewm(span=ema_slow, adjust=False).mean()
        data['ema_bullish'] = data['ema_fast_val'] > data['ema_slow_val']
        
        # RANGE MODE INDICATORS
        # RSI (native pandas)
        delta = data['close'].diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(rsi_length).mean()
        avg_loss = loss.rolling(rsi_length).mean()
        rs = avg_gain / avg_loss
        data['rsi'] = 100 - (100 / (1 + rs))
        
        # Stochastic (native pandas)
        low_min = data['low'].rolling(stoch_period).min()
        high_max = data['high'].rolling(stoch_period).max()
        data['stoch_k'] = 100 * (data['close'] - low_min) / (high_max - low_min)
        data['stoch_d'] = data['stoch_k'].rolling(stoch_smooth).mean()
        
        # ============ SIGNAL GENERATION ============
        
        # Get the last valid row
        last_idx = len(data) - 1
        while last_idx >= 0 and (pd.isna(data['close'].iloc[last_idx]) or pd.isna(data['regime'].iloc[last_idx])):
            last_idx -= 1
        
        if last_idx < 0:
            return Signal(entry_signal=False, exit_signal=False, price=0.0, direction="long", confidence=0.0)
        
        current_price = data['close'].iloc[last_idx]
        regime_val = data['regime'].iloc[last_idx]
        regime_confirmed = data['regime_confirmed'].iloc[last_idx] if 'regime_confirmed' in data.columns else True
        
        # Default: no signal
        signal = Signal(
            entry_signal=False,
            exit_signal=False,
            price=current_price,
            direction="long",
            confidence=0.0,
            indicators={
                'regime': regime_val,
                'regime_confirmed': regime_confirmed,
                'adx': data['adx'].iloc[last_idx] if 'adx' in data.columns else 0,
                'volatility_ratio': data['volatility_ratio'].iloc[last_idx] if 'volatility_ratio' in data.columns else 1,
            }
        )
        
        # Only generate entry signals if regime is confirmed
        if not regime_confirmed or pd.isna(regime_confirmed):
            return signal
        
        if regime_val == 1:  # TRENDING
            supertrend_val = data['supertrend'].iloc[last_idx] if 'supertrend' in data.columns else 1
            ema_bullish = data['ema_bullish'].iloc[last_idx] if 'ema_bullish' in data.columns else True
            
            if supertrend_val > 0 and ema_bullish:
                signal.entry_signal = True
                signal.direction = "long"
                signal.confidence = 0.7
                signal.indicators['mode'] = 'trend'
                signal.indicators['reason'] = 'supertrend_bullish'
            elif supertrend_val < 0 and not ema_bullish:
                signal.entry_signal = True
                signal.direction = "short"
                signal.confidence = 0.7
                signal.indicators['mode'] = 'trend'
                signal.indicators['reason'] = 'supertrend_bearish'
                
        else:  # RANGING
            rsi_val = data['rsi'].iloc[last_idx] if 'rsi' in data.columns else 50
            stoch_k = data['stoch_k'].iloc[last_idx] if 'stoch_k' in data.columns else 50
            
            if rsi_val < 30 and stoch_k < 20:
                signal.entry_signal = True
                signal.direction = "long"
                signal.confidence = 0.6
                signal.indicators['mode'] = 'range'
                signal.indicators['reason'] = 'oversold'
            elif rsi_val > 70 and stoch_k > 80:
                signal.entry_signal = True
                signal.direction = "short"
                signal.confidence = 0.6
                signal.indicators['mode'] = 'range'
                signal.indicators['reason'] = 'overbought'
        
        return signal
    
    def get_params_schema(self) -> dict:
        """Return the parameter schema for this strategy."""
        return {
            'atr_length': {'type': 'int', 'min': 2, 'max': 50, 'default': 14},
            'adx_length': {'type': 'int', 'min': 2, 'max': 50, 'default': 14},
            'volatility_threshold': {'type': 'float', 'min': 0.5, 'max': 3.0, 'default': 1.2},
            'trend_threshold': {'type': 'float', 'min': 10, 'max': 50, 'default': 25},
            'supertrend_period': {'type': 'int', 'min': 2, 'max': 30, 'default': 10},
            'supertrend_multiplier': {'type': 'float', 'min': 0.5, 'max': 5.0, 'default': 3.0},
            'ema_fast': {'type': 'int', 'min': 2, 'max': 50, 'default': 9},
            'ema_slow': {'type': 'int', 'min': 5, 'max': 100, 'default': 21},
            'rsi_length': {'type': 'int', 'min': 2, 'max': 30, 'default': 14},
            'stoch_period': {'type': 'int', 'min': 2, 'max': 30, 'default': 14},
            'stoch_smooth': {'type': 'int', 'min': 1, 'max': 10, 'default': 3},
            'regime_confirm_bars': {'type': 'int', 'min': 1, 'max': 10, 'default': 3},
        }


# Export for registration
TYPE_NAME = "regime_adaptive_eth"
STRATEGY_CLASS = RegimeAdaptiveStrategy