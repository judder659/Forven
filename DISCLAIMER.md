# Disclaimer

**Forven is experimental software for educational and research use. It is NOT financial advice, and it is provided AS IS, without warranty of any kind.**

## No financial advice

Nothing produced by Forven — strategies, backtests, signals, metrics, or autonomous decisions — is financial, investment, legal, or tax advice. You are solely responsible for every decision and every order.

## Substantial risk of loss

Trading cryptocurrencies and derivatives carries a substantial risk of loss, including the **total loss** of funds, and losses can exceed your initial deposit when leverage is involved. Only ever risk capital you can afford to lose entirely.

## Paper trading by default

Forven ships configured for **paper trading**: strategies trade with simulated fills against live market data, and no real money is at risk. **Hyperliquid testnet** is the supported default exchange connection. Live / mainnet (real-money) trading is **not a supported feature**: a live execution engine exists in the code but is **disabled by default** and reachable only through deliberate, multi-step opt-in (`FORVEN_EXECUTION_MODE=live` *and* `FORVEN_ALLOW_MAINNET=1` *and* mainnet credentials). The same applies to any feature that can send orders to a real account, such as live Bot Factory bots or mirroring strategies to a connected trading account. If you enable any of these, you do so entirely **at your own risk** — the authors do not support, endorse, or take responsibility for that use.

No strategy produced by Forven has been proven profitable in live trading. A strategy passing Forven's tests, or being offered for live trading, is not a recommendation to trade it.

## Backtest / paper performance is not predictive

Past, simulated, backtested, and paper-traded performance does **not** predict future or live results. The software's own performance metrics may be **inaccurate or misleading** (for example, due to data quality issues or look-ahead bias). Treat all reported numbers with skepticism and verify independently.

## Your keys, your responsibility

You are solely responsible for obtaining, funding, securing, and using your own exchange accounts, API keys, and model / LLM credentials, and for complying with all laws and regulations that apply to you — including securities, derivatives, and tax law in your jurisdiction.

## Eligibility & jurisdiction

Crypto and crypto-derivatives trading is restricted or prohibited for some persons and in some jurisdictions. You are solely responsible for determining whether you are legally eligible to use any exchange you connect, and for complying with all applicable laws, sanctions, and regulations (including those of your country of residence) and with each exchange's terms of service. Do not use Forven where doing so would be unlawful.

## No warranty / no liability

To the maximum extent permitted by law, Forven is provided **WITHOUT WARRANTY OF ANY KIND**, express or implied (see sections 15–16 of the AGPL-3.0 text in [`LICENSE`](LICENSE)). The authors and contributors accept **no liability** for any loss or damage of any kind arising from your use of the software. This file is an additional notice under AGPL-3.0 section 7(a); it does not modify the license itself.

By using Forven you acknowledge and accept these terms.
