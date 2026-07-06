"""Fixture module for XASSET-1 tests: classes whose SOURCE references
second-leg columns. Kept in its own module because the cross-asset detector
scans module source (helpers outside the class commonly read columns), so
these literals must not share a module with the clean fixtures."""


class CrossAssetLeadLag:
    """Fake strategy whose signal logic reads a second-leg column."""

    def __init__(self, strategy_id, params):
        self.params = params

    def data_requirements(self):
        return []

    def generate_signals(self, df):
        confirm = df["btc_close"]  # second leg — never joinable
        return confirm > df["close"]


class TwoAssetRequirements:
    def __init__(self, strategy_id, params):
        self.params = params

    def data_requirements(self):
        return [{"asset": "SOL"}, {"asset": "BTC"}]

    def generate_signals(self, df):
        return df["close"] > 0
