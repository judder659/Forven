"""Fixture module for XASSET-1 tests: single-asset classes with NO cross-asset
column literals anywhere in the module source."""


class PlainSingleAsset:
    def __init__(self, strategy_id, params):
        self.params = params

    def data_requirements(self):
        return []

    def generate_signals(self, df):
        return df["close"] > df["open"]


class OwnAssetOnly:
    def __init__(self, strategy_id, params):
        self.params = params

    def data_requirements(self):
        return [{"asset": "SOL/USDT"}]

    def generate_signals(self, df):
        return df["close"] > 0
