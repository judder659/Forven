/**
 * Comprehensive educational help content
 */

import type { HelpContent, HelpContentMap } from './types';

export const helpContent: HelpContentMap = {
	// ═══════════════════════════════════════════════════════════════════════════
	// PERFORMANCE METRICS
	// ═══════════════════════════════════════════════════════════════════════════

	sharpe_ratio: {
		id: 'sharpe_ratio',
		term: 'Sharpe Ratio',
		shortDescription: 'Risk-adjusted return measuring excess return per unit of volatility.',
		category: 'metric',
		fullDescription: `The Sharpe Ratio, developed by Nobel laureate William Sharpe in 1966, is the most widely used measure of risk-adjusted returns. It quantifies how much excess return you receive for the extra volatility you endure holding a riskier asset.

A higher Sharpe Ratio indicates better risk-adjusted performance. The ratio essentially answers: "Is this strategy's return worth the risk taken to achieve it?"

The key insight is that raw returns are meaningless without context. A 50% return sounds great, but not if the strategy experienced 80% drawdowns to achieve it. The Sharpe Ratio normalizes returns by their volatility, enabling fair comparison across different strategies and timeframes.`,
		formula: {
			latex: 'SR = \\frac{R_p - R_f}{\\sigma_p}',
			plain: 'SR = (Rp - Rf) / σp',
			variables: {
				'SR': 'Sharpe Ratio',
				'Rp': 'Portfolio/strategy return (annualized)',
				'Rf': 'Risk-free rate (typically 3-month T-bill rate, ~5% in 2024-2025)',
				'σp': 'Standard deviation of portfolio returns (annualized volatility)'
			}
		},
		interpretations: [
			{ range: '< 0', label: 'Negative', color: 'red', description: 'Strategy loses money or underperforms risk-free rate. Avoid.' },
			{ range: '0 - 0.5', label: 'Poor', color: 'red', description: 'Risk-adjusted return is weak. Not worth the volatility.' },
			{ range: '0.5 - 1.0', label: 'Acceptable', color: 'yellow', description: 'Decent but not exceptional. May be tradeable with low costs.' },
			{ range: '1.0 - 2.0', label: 'Good', color: 'green', description: 'Strong risk-adjusted returns. Most successful strategies fall here.' },
			{ range: '2.0 - 3.0', label: 'Excellent', color: 'green', description: 'Very strong performance. Rare in live trading.' },
			{ range: '> 3.0', label: 'Suspicious', color: 'blue', description: 'Extremely high. Verify for data errors, overfitting, or survivorship bias.' }
		],
		limitations: [
			'Assumes returns are normally distributed (they rarely are in trading)',
			'Penalizes upside volatility the same as downside volatility',
			'Sensitive to the time period chosen for calculation',
			'Can be manipulated by smoothing returns or using leverage',
			'Does not account for tail risk or black swan events',
			'Annualization assumes returns compound, which may not hold'
		],
		proTips: [
			'Always annualize Sharpe Ratios for comparison (multiply daily by √252, hourly by √8760)',
			'Compare strategies over the same time period - markets vary',
			'A backtest Sharpe above 2.0 often degrades to 0.5-1.0 in live trading',
			'Use Sortino Ratio alongside Sharpe to distinguish good vs bad volatility',
			'Institutional funds target Sharpe > 0.5; quant funds target > 1.0'
		],
		examples: [
			{
				scenario: 'Strategy A: 25% annual return, 20% volatility, 5% risk-free rate',
				calculation: 'SR = (25% - 5%) / 20% = 20% / 20%',
				result: 'Sharpe Ratio = 1.0',
				interpretation: 'Good risk-adjusted return. Each unit of risk produces one unit of excess return.'
			},
			{
				scenario: 'Strategy B: 40% annual return, 60% volatility, 5% risk-free rate',
				calculation: 'SR = (40% - 5%) / 60% = 35% / 60%',
				result: 'Sharpe Ratio = 0.58',
				interpretation: 'Despite higher raw return, Strategy A is superior on risk-adjusted basis.'
			}
		],
		relatedTerms: ['sortino_ratio', 'max_drawdown', 'volatility', 'calmar_ratio'],
		references: [
			'Sharpe, W.F. (1966). "Mutual Fund Performance." Journal of Business.',
			'Sharpe, W.F. (1994). "The Sharpe Ratio." Journal of Portfolio Management.'
		]
	},

	sortino_ratio: {
		id: 'sortino_ratio',
		term: 'Sortino Ratio',
		shortDescription: 'Risk-adjusted return using only downside volatility, not total volatility.',
		category: 'metric',
		fullDescription: `The Sortino Ratio is a modification of the Sharpe Ratio that only considers downside deviation (volatility of negative returns) rather than total standard deviation. This distinction is crucial because traders actually want upside volatility - big winning days are good!

Developed by Frank Sortino in the 1980s, this metric recognizes that not all volatility is created equal. A strategy that occasionally has 10% up days but never drops more than 2% should not be penalized the same as one that swings wildly in both directions.

For trading strategies, especially trend-following or momentum strategies that can have explosive upside, the Sortino Ratio often provides a more accurate picture of risk-adjusted performance.`,
		formula: {
			latex: 'Sortino = \\frac{R_p - R_f}{\\sigma_d}',
			plain: 'Sortino = (Rp - Rf) / σd',
			variables: {
				'Rp': 'Portfolio/strategy return (annualized)',
				'Rf': 'Risk-free rate or minimum acceptable return (MAR)',
				'σd': 'Downside deviation - standard deviation of returns below the target'
			}
		},
		interpretations: [
			{ range: '< 0', label: 'Negative', color: 'red', description: 'Strategy has negative excess returns. Avoid.' },
			{ range: '0 - 1.0', label: 'Poor', color: 'yellow', description: 'Downside risk not adequately compensated.' },
			{ range: '1.0 - 2.0', label: 'Good', color: 'green', description: 'Healthy reward for downside risk taken.' },
			{ range: '2.0 - 3.0', label: 'Excellent', color: 'green', description: 'Strong downside-adjusted performance.' },
			{ range: '> 3.0', label: 'Outstanding', color: 'blue', description: 'Exceptional if verified. Check for data issues.' }
		],
		limitations: [
			'Requires more data points than Sharpe for statistical significance',
			'Definition of "downside" (target return) can be subjective',
			'Less widely reported, making comparison across sources difficult',
			'Can be misleading in strong bull markets with few down periods',
			'Still does not capture tail risk or maximum drawdown severity'
		],
		proTips: [
			'Sortino is typically 1.4-1.5x the Sharpe for asymmetric strategies',
			'If Sortino ≈ Sharpe, the strategy has symmetric volatility (equal up/down moves)',
			'If Sortino >> Sharpe, the strategy has positive skew (big winners, small losers)',
			'Use MAR = 0% for trading strategies, not the risk-free rate',
			'Combine with Max Drawdown for complete downside risk picture'
		],
		examples: [
			{
				scenario: 'Strategy with 30% return, 5% target, 15% downside deviation',
				calculation: 'Sortino = (30% - 5%) / 15%',
				result: 'Sortino Ratio = 1.67',
				interpretation: 'Good downside-adjusted return. Upside volatility not penalized.'
			}
		],
		relatedTerms: ['sharpe_ratio', 'max_drawdown', 'downside_deviation', 'calmar_ratio'],
		references: [
			'Sortino, F.A. & Price, L.N. (1994). "Performance Measurement in a Downside Risk Framework." Journal of Investing.'
		]
	},

	max_drawdown: {
		id: 'max_drawdown',
		term: 'Maximum Drawdown',
		shortDescription: 'Largest peak-to-trough decline in portfolio value.',
		category: 'metric',
		fullDescription: `Maximum Drawdown (MDD) measures the largest percentage drop from a peak to a subsequent trough before a new peak is achieved. It represents the worst-case loss an investor would have experienced if they bought at the top and sold at the bottom.

This is arguably the most important risk metric for practical trading because:
1. It directly measures the pain you'll experience
2. It determines how much capital you need to survive
3. It affects your psychology and ability to stick with a strategy

A 50% drawdown requires a 100% gain just to break even. A 75% drawdown requires a 300% gain. This asymmetry is why drawdown management is critical.`,
		formula: {
			latex: 'MDD = \\frac{Trough - Peak}{Peak} \\times 100\\%',
			plain: 'MDD = (Trough - Peak) / Peak × 100%',
			variables: {
				'MDD': 'Maximum Drawdown (expressed as negative percentage)',
				'Peak': 'Highest portfolio value before the decline',
				'Trough': 'Lowest portfolio value after the peak, before recovery'
			}
		},
		interpretations: [
			{ range: '0 - 10%', label: 'Low', color: 'green', description: 'Conservative strategy. Suitable for risk-averse investors.' },
			{ range: '10 - 20%', label: 'Moderate', color: 'green', description: 'Typical for well-managed strategies. Recoverable.' },
			{ range: '20 - 30%', label: 'Elevated', color: 'yellow', description: 'Significant but acceptable for higher-return strategies.' },
			{ range: '30 - 50%', label: 'High', color: 'yellow', description: 'Requires strong conviction. Many traders abandon here.' },
			{ range: '> 50%', label: 'Severe', color: 'red', description: 'Dangerous. Recovery very difficult. Rethink strategy.' }
		],
		limitations: [
			'Historical MDD is the minimum you can expect - future may be worse',
			'Does not indicate how long recovery takes (could be months or years)',
			'Single metric hides important details about frequency and duration',
			'Can be misleading if peak was an anomaly or bubble',
			'Does not account for emotional impact of watching losses accumulate'
		],
		proTips: [
			'Expect 1.5-2x backtest MDD in live trading due to slippage and bad timing',
			'Rule of thumb: If you cannot stomach 2x the backtest MDD, reduce position size',
			'Track both MDD and time-to-recovery (underwater period)',
			'Use stop-losses or position sizing to cap maximum possible drawdown',
			'Crypto strategies often see 30-50% MDD even for profitable strategies'
		],
		examples: [
			{
				scenario: 'Portfolio grows to $15,000, drops to $10,500, recovers to $18,000',
				calculation: 'MDD = ($10,500 - $15,000) / $15,000',
				result: 'Maximum Drawdown = -30%',
				interpretation: 'Despite ending higher, investor experienced 30% loss at worst point.'
			}
		],
		relatedTerms: ['calmar_ratio', 'sharpe_ratio', 'recovery_time', 'risk_of_ruin'],
		references: [
			'Magdon-Ismail, M. et al. (2004). "On the Maximum Drawdown of a Brownian Motion." Journal of Applied Probability.'
		]
	},

	profit_factor: {
		id: 'profit_factor',
		term: 'Profit Factor',
		shortDescription: 'Ratio of gross profits to gross losses.',
		category: 'metric',
		fullDescription: `Profit Factor is a simple but powerful metric that divides gross profits by gross losses. A Profit Factor of 2.0 means the strategy makes $2 for every $1 it loses.

This metric is popular among traders because it's intuitive and directly relates to the bottom line. Unlike Sharpe Ratio, which can be gamed by smoothing returns, Profit Factor is harder to manipulate.

However, Profit Factor alone doesn't tell the whole story. A strategy could have a high Profit Factor but trade very infrequently, or have a few lucky big wins that skew the ratio.`,
		formula: {
			latex: 'PF = \\frac{\\sum Winning\\ Trades}{|\\sum Losing\\ Trades|}',
			plain: 'PF = Gross Profits / Gross Losses',
			variables: {
				'PF': 'Profit Factor',
				'Gross Profits': 'Sum of all profitable trades',
				'Gross Losses': 'Absolute value of sum of all losing trades'
			}
		},
		interpretations: [
			{ range: '< 1.0', label: 'Losing', color: 'red', description: 'Strategy loses money. Losses exceed profits.' },
			{ range: '1.0 - 1.2', label: 'Marginal', color: 'yellow', description: 'Barely profitable. Costs may eliminate edge.' },
			{ range: '1.2 - 1.5', label: 'Acceptable', color: 'yellow', description: 'Modest edge. Tradeable with tight cost control.' },
			{ range: '1.5 - 2.0', label: 'Good', color: 'green', description: 'Solid edge. Most successful strategies are here.' },
			{ range: '2.0 - 3.0', label: 'Excellent', color: 'green', description: 'Strong edge. Verify it is not overfit.' },
			{ range: '> 3.0', label: 'Suspicious', color: 'blue', description: 'Unusually high. Check for curve-fitting or data errors.' }
		],
		limitations: [
			'Ignores trade frequency - high PF with 5 trades is unreliable',
			'Does not account for trade size distribution',
			'Can be skewed by one or two outlier trades',
			'Does not measure risk - a strategy could have high PF but huge drawdowns',
			'Sensitive to classification of trades (e.g., partial fills)'
		],
		proTips: [
			'Minimum 30 trades needed for statistical significance',
			'Check PF by year/month to ensure consistency',
			'High PF with low win rate = few big winners (trend following)',
			'Lower PF with high win rate = many small winners (mean reversion)',
			'After costs, target PF > 1.3 for practical edge'
		],
		examples: [
			{
				scenario: '10 winning trades totaling $5,000, 15 losing trades totaling -$3,000',
				calculation: 'PF = $5,000 / $3,000',
				result: 'Profit Factor = 1.67',
				interpretation: 'Good profit factor. Strategy makes $1.67 for every $1 risked.'
			}
		],
		relatedTerms: ['win_rate', 'avg_win_loss_ratio', 'expectancy', 'total_trades'],
		references: [
			'Pardo, R. (2008). "The Evaluation and Optimization of Trading Strategies." Wiley.'
		]
	},

	win_rate: {
		id: 'win_rate',
		term: 'Win Rate',
		shortDescription: 'Percentage of trades that are profitable.',
		category: 'metric',
		fullDescription: `Win Rate (also called Win Percentage or Hit Rate) is the percentage of trades that close profitably. While intuitive, win rate alone is one of the most misunderstood metrics in trading.

A high win rate feels good psychologically but doesn't guarantee profitability. A strategy winning 90% of trades but losing 10x more on losses than it makes on wins will be unprofitable. Conversely, trend-following strategies often have win rates of 30-40% but remain highly profitable because winners are much larger than losers.

The relationship between win rate and average win/loss ratio determines expectancy - the true measure of a strategy's edge.`,
		formula: {
			latex: 'Win\\ Rate = \\frac{Winning\\ Trades}{Total\\ Trades} \\times 100\\%',
			plain: 'Win Rate = (Winning Trades / Total Trades) × 100%',
			variables: {
				'Winning Trades': 'Number of trades closed with profit > 0',
				'Total Trades': 'Total number of closed trades'
			}
		},
		interpretations: [
			{ range: '< 30%', label: 'Low', color: 'yellow', description: 'Requires large win/loss ratio (>3:1) to be profitable.' },
			{ range: '30 - 45%', label: 'Typical Trend', color: 'gray', description: 'Normal for trend-following. Check avg win/loss ratio.' },
			{ range: '45 - 55%', label: 'Balanced', color: 'gray', description: 'Moderate. Works with 1.2:1+ win/loss ratio.' },
			{ range: '55 - 70%', label: 'Typical MR', color: 'gray', description: 'Normal for mean-reversion strategies.' },
			{ range: '> 70%', label: 'High', color: 'blue', description: 'Very high. Verify not picking up pennies before steamroller.' }
		],
		limitations: [
			'Completely ignores trade magnitude - useless without avg win/loss ratio',
			'Can create false confidence in unprofitable strategies',
			'Sensitive to trade classification and partial positions',
			'Market conditions affect achievable win rate',
			'Psychological bias toward high win rate can hurt returns'
		],
		proTips: [
			'Win Rate × Avg Win/Loss Ratio > 1 is the minimum for profitability',
			'Expectancy = (Win Rate × Avg Win) - (Loss Rate × Avg Loss)',
			'Trend strategies: accept 35% win rate if winners are 3x losers',
			'Mean reversion: 60%+ win rate normal, but watch for tail risk',
			'Track win rate by market regime - it will vary significantly'
		],
		examples: [
			{
				scenario: 'Strategy A: 70% win rate, avg win $100, avg loss $300',
				calculation: 'Expectancy = (0.70 × $100) - (0.30 × $300) = $70 - $90',
				result: 'Expectancy = -$20 per trade',
				interpretation: 'Despite high win rate, strategy is LOSING money!'
			},
			{
				scenario: 'Strategy B: 35% win rate, avg win $400, avg loss $100',
				calculation: 'Expectancy = (0.35 × $400) - (0.65 × $100) = $140 - $65',
				result: 'Expectancy = +$75 per trade',
				interpretation: 'Low win rate but highly profitable due to large winners.'
			}
		],
		relatedTerms: ['profit_factor', 'expectancy', 'avg_win_loss_ratio', 'total_trades'],
		references: [
			'Van Tharp, K. (2006). "Trade Your Way to Financial Freedom." McGraw-Hill.'
		]
	},

	total_return: {
		id: 'total_return',
		term: 'Total Return',
		shortDescription: 'Overall percentage gain or loss over the backtest period.',
		category: 'metric',
		fullDescription: `Total Return represents the overall percentage gain or loss of a strategy over the entire backtest period. It's the most basic performance metric but must be contextualized with time period, risk taken, and market conditions.

A 100% return sounds impressive until you learn it took 10 years (roughly 7% annually) or came with 80% drawdowns. Always pair total return with time period and risk metrics for meaningful analysis.`,
		formula: {
			latex: 'Total\\ Return = \\frac{Final\\ Value - Initial\\ Value}{Initial\\ Value} \\times 100\\%',
			plain: 'Total Return = ((Final - Initial) / Initial) × 100%',
			variables: {
				'Final Value': 'Portfolio value at end of period',
				'Initial Value': 'Starting capital'
			}
		},
		interpretations: [
			{ range: '< 0%', label: 'Loss', color: 'red', description: 'Strategy lost money. Investigate why.' },
			{ range: '0 - 10%', label: 'Low', color: 'yellow', description: 'May not beat risk-free alternatives.' },
			{ range: '10 - 50%', label: 'Moderate', color: 'gray', description: 'Context-dependent. Check timeframe.' },
			{ range: '50 - 100%', label: 'Good', color: 'green', description: 'Strong if achieved with reasonable risk.' },
			{ range: '> 100%', label: 'High', color: 'green', description: 'Excellent if sustainable and not overfit.' }
		],
		limitations: [
			'Meaningless without timeframe context',
			'Ignores the path taken (volatility and drawdowns)',
			'Does not account for risk taken to achieve return',
			'Can be dominated by a few exceptional periods'
		],
		proTips: [
			'Always annualize for comparison: (1 + Total Return)^(1/years) - 1',
			'Compare to buy-and-hold of the same asset',
			'Divide by max drawdown for rough risk-adjusted view',
			'Check return consistency across years, not just total'
		],
		examples: [
			{
				scenario: '$10,000 grows to $15,000 over 2 years',
				calculation: 'Total Return = ($15,000 - $10,000) / $10,000 = 50%',
				result: 'Annualized ≈ 22.5%',
				interpretation: 'Good return, but need to check risk metrics.'
			}
		],
		relatedTerms: ['cagr', 'sharpe_ratio', 'max_drawdown', 'benchmark_return'],
		references: []
	},

	// ═══════════════════════════════════════════════════════════════════════════
	// WALK-FORWARD ANALYSIS
	// ═══════════════════════════════════════════════════════════════════════════

	walk_forward_analysis: {
		id: 'walk_forward_analysis',
		term: 'Walk-Forward Analysis',
		shortDescription: 'Rolling optimization and out-of-sample testing to detect overfitting.',
		category: 'walkforward',
		fullDescription: `Walk-Forward Analysis (WFA) is a robust validation technique that simulates how a strategy would actually be traded: optimize on historical data, trade the next period, then re-optimize with new data, and repeat.

Unlike a single backtest where the strategy sees all data at once (leading to overfitting), WFA forces the strategy to prove itself on truly unseen data multiple times. This is the closest approximation to live trading performance you can achieve in backtesting.

The process divides history into multiple rolling windows. Each window has a training period (for optimization) and a test period (for validation). The combined out-of-sample results reveal how well optimization generalizes.`,
		formula: {
			latex: 'Overfitting\\ Ratio = \\frac{Train\\ Performance}{Test\\ Performance}',
			plain: 'Overfitting Ratio = Train Performance / Test Performance',
			variables: {
				'Train Performance': 'Average metric (e.g., Sharpe) across all training periods',
				'Test Performance': 'Average metric across all out-of-sample test periods',
				'Overfitting Ratio': 'Values > 1 indicate overfitting; 1.0 is ideal'
			}
		},
		interpretations: [
			{ range: '0.8 - 1.2', label: 'Excellent', color: 'green', description: 'Little overfitting. Strategy generalizes well.' },
			{ range: '1.2 - 1.5', label: 'Acceptable', color: 'yellow', description: 'Moderate overfitting. Use with caution.' },
			{ range: '1.5 - 2.0', label: 'Concerning', color: 'yellow', description: 'Significant overfitting detected.' },
			{ range: '> 2.0', label: 'Severe', color: 'red', description: 'Heavy overfitting. Do not trade this strategy.' }
		],
		limitations: [
			'Requires sufficient data - minimum 3-5 years for most timeframes',
			'Computationally expensive (runs full optimization multiple times)',
			'Results sensitive to fold size and window parameters',
			'Does not guarantee future performance - just reduces false confidence',
			'Can still miss regime changes not present in historical data'
		],
		proTips: [
			'Use at least 5 folds for statistical significance',
			'Training period should be 3-5x longer than test period',
			'Look for consistent performance across ALL folds, not just average',
			'Run WFA with different window sizes - robust strategies hold up',
			'Combine with Monte Carlo for tail-risk context — WFA tests edge existence, MC tests path stability'
		],
		examples: [
			{
				scenario: '5-fold WFA on 3 years of data',
				calculation: 'Train Sharpe: [1.8, 2.1, 1.9, 2.0, 1.7], Test Sharpe: [1.2, 0.9, 1.1, 0.8, 1.0]',
				result: 'Avg Train: 1.9, Avg Test: 1.0, Ratio: 1.9',
				interpretation: 'Significant overfitting. In-sample looks great but degrades 50% out-of-sample.'
			}
		],
		relatedTerms: ['overfitting', 'purge_gap', 'embargo', 'train_test_split', 'cross_validation'],
		references: [
			'Pardo, R. (2008). "The Evaluation and Optimization of Trading Strategies." Wiley.',
			'Bailey, D.H. et al. (2014). "The Probability of Backtest Overfitting." Journal of Computational Finance.'
		]
	},

	overfitting: {
		id: 'overfitting',
		term: 'Overfitting',
		shortDescription: 'When a strategy performs well on past data but fails on new data.',
		category: 'walkforward',
		fullDescription: `Overfitting occurs when a trading strategy is too finely tuned to historical data, capturing noise rather than genuine patterns. The strategy appears profitable in backtests but fails in live trading because it has essentially "memorized" the past rather than learned generalizable rules.

Signs of overfitting:
- Backtest Sharpe > 2.0 that degrades significantly in walk-forward
- Many optimized parameters (more than 3-5)
- Parameters at extreme values or boundaries
- Performance depends heavily on exact parameter values
- Strategy stops working shortly after deployment

Overfitting is the #1 reason trading strategies fail in production. A 2016 study found that more than 50% of published "alpha" strategies could not be replicated.`,
		interpretations: [
			{ range: 'Ratio < 1.2', label: 'Low Risk', color: 'green', description: 'Good generalization. Train and test performance similar.' },
			{ range: 'Ratio 1.2-1.5', label: 'Moderate Risk', color: 'yellow', description: 'Some overfitting. Reduce parameters or simplify.' },
			{ range: 'Ratio 1.5-2.0', label: 'High Risk', color: 'yellow', description: 'Significant overfitting detected. Reconsider strategy.' },
			{ range: 'Ratio > 2.0', label: 'Severe', color: 'red', description: 'Heavily overfit. Do not deploy.' }
		],
		limitations: [
			'Can be hard to detect if test set is small',
			'Some overfitting is unavoidable with any optimization',
			'Distinguishing signal from noise is inherently difficult',
			'Market regimes can make valid strategies look overfit'
		],
		proTips: [
			'Use the fewest parameters possible that still capture the edge',
			'Prefer robust strategies that work across parameter ranges',
			'Test on different markets, timeframes, and time periods',
			'Walk-forward analysis is the best tool for detection',
			'If in doubt, use default or literature-suggested parameters'
		],
		examples: [
			{
				scenario: 'RSI strategy optimized with 15 parameters to Sharpe 3.5',
				calculation: 'Walk-forward shows Test Sharpe of 0.3',
				result: 'Overfitting Ratio = 11.7',
				interpretation: 'Extreme overfitting. Strategy has no real edge.'
			}
		],
		relatedTerms: ['walk_forward_analysis', 'in_sample', 'out_of_sample', 'bias_variance_tradeoff'],
		references: [
			'Bailey, D.H. et al. (2014). "The Probability of Backtest Overfitting." Journal of Computational Finance.',
			'López de Prado, M. (2018). "Advances in Financial Machine Learning." Wiley.'
		]
	},

	monte_carlo: {
		id: 'monte_carlo',
		term: 'Monte Carlo Simulation',
		shortDescription: 'Bootstrap of realized trade returns measuring path stability and tail risk — not edge existence.',
		category: 'risk',
		fullDescription: `Forven's Monte Carlo test resamples the trades from a baseline backtest (bootstrap with replacement) thousands of times, building alternate equity paths from the same trade set in different orders. The distributions of final return and maximum drawdown reveal how much of the backtest's outcome depends on the lucky sequencing of one historical path.

What it tests: given the trades this strategy actually produced, how bad can the drawdown tail get, and how often does a reshuffled path still end profitable? The verdict requires a minimum share of profitable resamples and caps the 95th-percentile drawdown.

What it does NOT test: whether the underlying edge is real. The bootstrap assumes the trade set is valid and only randomizes its ordering — a curve-fitted strategy with a lucky trade set will pass Monte Carlo easily. Edge existence is tested by walk-forward analysis (unseen future data), regime split (does it survive different market conditions), and the deflated Sharpe guard (selection bias). A Monte Carlo PASS means "the trade sequence is stable and ruin risk is bounded", never "the edge is proven".`,
		interpretations: [
			{ range: 'Prob profitable ≥ 65%', label: 'Pass floor', color: 'green', description: 'Most reshuffled paths stay profitable — the outcome is not one lucky ordering.' },
			{ range: 'P95 max DD ≤ 40%', label: 'Tail cap', color: 'green', description: 'Even the bad tail of reshuffled paths keeps drawdown inside the ceiling.' },
			{ range: 'P95 max DD > 40%', label: 'Tail risk', color: 'red', description: 'A plausible reordering of the same trades breaches the drawdown ceiling.' }
		],
		limitations: [
			'Assumes the baseline trade set is valid — cannot detect an overfit or curve-fitted edge',
			'Resampling breaks serial correlation between trades (clustered losses may be understated)',
			'Only randomizes trade ordering, not entry timing against the price series',
			'Results are conditional on the backtest window; a different window gives a different trade set'
		],
		proTips: [
			'Read a PASS as "path-stable and tail-bounded", not as evidence of edge',
			'Pair with walk-forward analysis and regime split — those test edge existence',
			'Watch P95 max drawdown even on a PASS: it is a better sizing guide than the backtest’s single realized drawdown'
		],
		examples: [
			{
				scenario: '1000 resamples of a 60-trade baseline with +18% total return',
				calculation: '82% of resampled paths end profitable; P95 max drawdown = 24%',
				result: 'PASS',
				interpretation: 'The trade sequence is robust to reordering and tail risk is bounded. Says nothing about whether the entries have predictive power.'
			}
		],
		relatedTerms: ['walk_forward_analysis', 'overfitting', 'max_drawdown'],
		references: [
			'Aronson, D. (2006). "Evidence-Based Technical Analysis." Wiley.'
		]
	},

	purge_gap: {
		id: 'purge_gap',
		term: 'Purge Gap',
		shortDescription: 'Buffer period removed between train and test sets to prevent data leakage.',
		category: 'walkforward',
		fullDescription: `The Purge Gap is a buffer period of data that is excluded from analysis between the training and test sets in walk-forward analysis. This gap prevents information leakage that could artificially inflate test performance.

Why is this necessary? Many indicators use lookback windows. If an indicator uses a 20-period lookback and the test period starts immediately after training, the test period's early signals are computed using data that was optimized on. This subtle leakage can make overfit strategies appear robust.

The purge gap should be at least as long as the longest lookback period used in the strategy.`,
		formula: {
			latex: 'Purge\\ Gap \\geq max(lookback\\ periods)',
			plain: 'Purge Gap ≥ longest indicator lookback period',
			variables: {
				'Purge Gap': 'Number of bars excluded between train and test',
				'Lookback': 'Longest period used by any indicator (e.g., 200 SMA = 200 bars)'
			}
		},
		interpretations: [
			{ range: '0 bars', label: 'None', color: 'red', description: 'Data leakage likely. Results unreliable.' },
			{ range: '< max lookback', label: 'Insufficient', color: 'yellow', description: 'Partial leakage possible.' },
			{ range: '= max lookback', label: 'Minimum', color: 'green', description: 'Basic protection against leakage.' },
			{ range: '> max lookback', label: 'Conservative', color: 'green', description: 'Extra safety margin. Recommended.' }
		],
		limitations: [
			'Reduces total data available for analysis',
			'Can be hard to determine correct size for complex strategies',
			'Does not prevent all forms of data leakage',
			'Some argue it is overly conservative for certain strategies'
		],
		proTips: [
			'Use 2x your longest lookback for extra safety',
			'Document the lookback periods of all indicators',
			'For daily data with 200 SMA, use at least 200 day purge gap',
			'Consider regime persistence when setting gap size'
		],
		examples: [
			{
				scenario: 'Strategy uses 50 SMA, 200 SMA, and 14 RSI',
				calculation: 'Max lookback = 200 periods',
				result: 'Purge Gap = 200 bars minimum',
				interpretation: 'First 200 bars of test set would use train data in indicators.'
			}
		],
		relatedTerms: ['walk_forward_analysis', 'embargo', 'data_leakage', 'lookback_period'],
		references: [
			'López de Prado, M. (2018). "Advances in Financial Machine Learning." Wiley.'
		]
	},

	embargo: {
		id: 'embargo',
		term: 'Embargo Period',
		shortDescription: 'Additional buffer after test period to prevent forward-looking leakage.',
		category: 'walkforward',
		fullDescription: `The Embargo Period is an additional buffer placed AFTER the test set, before the next training period begins. While the purge gap prevents backward-looking leakage, the embargo prevents forward-looking leakage.

This is particularly important when:
1. Labels or targets use future information (e.g., "price up 5% in next 10 days")
2. Strategy exits depend on future price action
3. Returns are computed over multiple periods

The embargo ensures that information from future folds cannot leak into the current test period through overlapping return calculations or target labels.`,
		formula: {
			latex: 'Embargo \\geq max(holding\\ period,\\ label\\ horizon)',
			plain: 'Embargo ≥ max(holding period, label horizon)',
			variables: {
				'Embargo': 'Bars excluded after test, before next train',
				'Holding Period': 'Average or maximum trade duration',
				'Label Horizon': 'How far forward labels/targets look'
			}
		},
		interpretations: [
			{ range: '0 bars', label: 'None', color: 'yellow', description: 'May be acceptable for simple strategies.' },
			{ range: '< holding period', label: 'Insufficient', color: 'yellow', description: 'Return leakage possible for long trades.' },
			{ range: '≥ holding period', label: 'Adequate', color: 'green', description: 'Properly prevents forward leakage.' }
		],
		limitations: [
			'Further reduces usable data',
			'May not be necessary for all strategy types',
			'Overkill for simple entry/exit signals without forward labels',
			'Can interact complexly with purge gap sizing'
		],
		proTips: [
			'For ML strategies with forward labels, embargo is critical',
			'For simple indicator strategies, embargo can often be skipped',
			'When in doubt, add an embargo equal to your average trade duration',
			'Combined with purge gap, you may lose significant data - plan accordingly'
		],
		examples: [
			{
				scenario: 'Strategy trades with 10-day average holding period',
				calculation: 'Embargo = 10 bars',
				result: 'Prevents fold N test returns leaking into fold N+1 training',
				interpretation: 'Returns from late test period trades do not influence next training.'
			}
		],
		relatedTerms: ['purge_gap', 'walk_forward_analysis', 'data_leakage', 'holding_period'],
		references: [
			'López de Prado, M. (2018). "Advances in Financial Machine Learning." Wiley.'
		]
	},

	cv_method: {
		id: 'cv_method',
		term: 'Cross-Validation Method',
		shortDescription: 'Strategy for splitting data into training and testing periods.',
		category: 'walkforward',
		fullDescription: `Cross-Validation (CV) methods determine how historical data is divided into training and testing segments. The choice of CV method significantly impacts the validity of walk-forward analysis results.

Common methods for time series:

**Rolling Window**: Fixed-size training window moves forward through time. Most realistic as it mimics limited memory / look-back constraint of live trading.

**Expanding Window**: Training set grows with each fold. Uses all available prior data. May capture more patterns but assumes stationarity.

**Anchored**: Training always starts from the same point but end expands. Good for capturing long-term patterns while testing recency.

Unlike standard ML cross-validation (like k-fold), time-series CV must respect temporal order - you cannot train on future data.`,
		interpretations: [
			{ range: 'Rolling', label: 'Realistic', color: 'green', description: 'Best mimics live trading with fixed lookback.' },
			{ range: 'Expanding', label: 'Data Efficient', color: 'gray', description: 'Uses all data but assumes patterns persist.' },
			{ range: 'Anchored', label: 'Hybrid', color: 'gray', description: 'Balance of recency and history.' }
		],
		limitations: [
			'Rolling window discards old data that may still be relevant',
			'Expanding window gives different data amounts to different folds',
			'All methods assume some degree of stationarity',
			'Results can vary significantly based on CV method choice'
		],
		proTips: [
			'Use rolling window for strategies that adapt to recent markets',
			'Use expanding window if you believe past patterns persist',
			'Run both methods and compare - robust strategies work in both',
			'Ensure each fold has enough data for statistical significance'
		],
		examples: [
			{
				scenario: '5 years of data, 5 folds',
				calculation: 'Rolling: 2yr train, 6mo test each fold. Expanding: 1yr/2yr/3yr/4yr train, 6mo test.',
				result: 'Rolling keeps consistent train size; Expanding grows it',
				interpretation: 'Rolling is preferred for adapting to market regime changes.'
			}
		],
		relatedTerms: ['walk_forward_analysis', 'train_test_split', 'rolling_window', 'time_series_split'],
		references: [
			'Arlot, S. & Celisse, A. (2010). "A survey of cross-validation procedures." Statistics Surveys.'
		]
	},

	// ═══════════════════════════════════════════════════════════════════════════
	// OPTIMIZATION
	// ═══════════════════════════════════════════════════════════════════════════

	parameter_optimization: {
		id: 'parameter_optimization',
		term: 'Parameter Optimization',
		shortDescription: 'Systematic search for best strategy parameter values.',
		category: 'optimization',
		fullDescription: `Parameter optimization is the process of systematically testing different parameter combinations to find values that maximize a chosen objective (typically risk-adjusted return).

Methods include:
- **Grid Search**: Test all combinations in a predefined grid. Exhaustive but slow.
- **Random Search**: Test random combinations. Often more efficient than grid.
- **Bayesian Optimization**: Use probabilistic model to guide search. Most efficient for expensive objectives.
- **Genetic Algorithms**: Evolve parameters using selection and mutation.

The danger is that optimization can find parameters that worked historically by chance (overfitting). Walk-forward analysis helps validate that optimized parameters generalize.`,
		interpretations: [
			{ range: 'Grid', label: 'Thorough', color: 'gray', description: 'Complete but computationally expensive.' },
			{ range: 'Random', label: 'Efficient', color: 'gray', description: 'Good coverage with fewer iterations.' },
			{ range: 'Bayesian', label: 'Smart', color: 'green', description: 'Learns from previous trials. Best for complex spaces.' },
			{ range: 'Genetic', label: 'Adaptive', color: 'gray', description: 'Good for very large parameter spaces.' }
		],
		limitations: [
			'More parameters = exponentially more combinations = more overfitting risk',
			'Optimal parameters may not be stable over time',
			'Local optima can trap search algorithms',
			'Results depend heavily on optimization objective choice'
		],
		proTips: [
			'Limit to 3-5 parameters maximum for robust strategies',
			'Use coarse-to-fine search: wide grid first, then narrow',
			'Optimize for Sharpe or Sortino, not raw return',
			'Check parameter stability - nearby values should have similar performance',
			'Always validate with walk-forward analysis'
		],
		examples: [
			{
				scenario: 'RSI strategy with period and threshold parameters',
				calculation: 'Grid: period [10, 14, 20, 30], threshold [20, 25, 30]',
				result: '12 combinations tested',
				interpretation: 'Manageable search space with reasonable overfitting risk.'
			}
		],
		relatedTerms: ['walk_forward_analysis', 'overfitting', 'grid_search', 'bayesian_optimization'],
		references: [
			'Bergstra, J. & Bengio, Y. (2012). "Random Search for Hyper-Parameter Optimization." JMLR.'
		]
	},

	optuna: {
		id: 'optuna',
		term: 'Optuna',
		shortDescription: 'Hyperparameter optimization framework using Bayesian and Tree-structured methods.',
		category: 'optimization',
		fullDescription: `Optuna is an automatic hyperparameter optimization framework that uses sophisticated algorithms to efficiently search parameter spaces. It's particularly well-suited for trading strategy optimization because:

1. **Pruning**: Stops unpromising trials early, saving computation
2. **TPE (Tree-structured Parzen Estimator)**: Learns from past trials to suggest better parameters
3. **Multi-objective**: Can optimize for multiple goals simultaneously (e.g., return AND risk)
4. **Visualization**: Built-in tools to understand parameter importance and interactions

Forven uses Optuna for parameter optimization, providing efficient search without exhaustive grid testing.`,
		proTips: [
			'Start with 100+ trials for reliable optimization',
			'Enable pruning to speed up by 2-5x',
			'Use suggest_int and suggest_float for parameter types',
			'Check optimization history for convergence',
			'Multi-objective mode can optimize Sharpe and Drawdown simultaneously'
		],
		relatedTerms: ['parameter_optimization', 'bayesian_optimization', 'tpe', 'hyperparameter'],
		references: [
			'Akiba, T. et al. (2019). "Optuna: A Next-generation Hyperparameter Optimization Framework." KDD.'
		]
	},

	// ═══════════════════════════════════════════════════════════════════════════
	// DATA CONCEPTS
	// ═══════════════════════════════════════════════════════════════════════════

	ohlcv: {
		id: 'ohlcv',
		term: 'OHLCV Data',
		shortDescription: 'Open, High, Low, Close, Volume - standard price data format.',
		category: 'data',
		fullDescription: `OHLCV is the standard format for financial price data:

- **Open**: Price at period start
- **High**: Highest price during period
- **Low**: Lowest price during period
- **Close**: Price at period end
- **Volume**: Amount traded during period

This data structure captures the full price range and activity for each time period, enabling technical analysis and backtesting. Most indicators and strategies rely on these five values.`,
		proTips: [
			'Close price is most commonly used for indicators',
			'High-Low range indicates volatility',
			'Volume confirms price moves (high volume = stronger conviction)',
			'Check for gaps between close and next open',
			'Ensure data quality - missing candles cause calculation errors'
		],
		relatedTerms: ['candlestick', 'timeframe', 'volume', 'price_action'],
		references: []
	},

	timeframe: {
		id: 'timeframe',
		term: 'Timeframe',
		shortDescription: 'Duration of each OHLCV candle (1m, 5m, 1h, 4h, 1d, etc.).',
		category: 'data',
		fullDescription: `Timeframe determines the duration each candlestick/bar represents. Common timeframes:

- **Scalping**: 1m, 5m (high noise, many signals)
- **Intraday**: 15m, 1h (balance of signal quality and frequency)
- **Swing**: 4h, 1d (cleaner signals, longer holds)
- **Position**: 1w, 1M (macro trends, low frequency)

The optimal timeframe depends on your strategy, trading costs, and availability. Higher timeframes have less noise but fewer trading opportunities. Lower timeframes allow more trades but higher costs and noise.`,
		proTips: [
			'Start development on 1h or 4h - good balance of data and noise',
			'Higher timeframes need more history for the same statistical significance',
			'Trading costs matter more on lower timeframes',
			'Some patterns only appear on certain timeframes',
			'Multi-timeframe analysis uses higher TF for trend, lower for entry'
		],
		relatedTerms: ['ohlcv', 'trading_costs', 'signal_frequency', 'noise'],
		references: []
	},

	// ═══════════════════════════════════════════════════════════════════════════
	// STRATEGY CONCEPTS
	// ═══════════════════════════════════════════════════════════════════════════

	mean_reversion: {
		id: 'mean_reversion',
		term: 'Mean Reversion',
		shortDescription: 'Strategy betting prices will return to average after deviating.',
		category: 'strategy',
		fullDescription: `Mean Reversion strategies bet that prices tend to return to some average or equilibrium level after deviating. When price moves "too far" from the mean, these strategies enter in the opposite direction expecting a snapback.

Common mean reversion indicators:
- **RSI** (Relative Strength Index): Buy when oversold (<30), sell when overbought (>70)
- **Bollinger Bands**: Buy at lower band, sell at upper band
- **CCI** (Commodity Channel Index): Similar to RSI concept

Mean reversion works best in ranging/choppy markets but can be devastating in strong trends where price keeps moving away from the mean.`,
		proTips: [
			'Best in sideways, ranging markets',
			'Terrible in strong trends - the mean keeps shifting',
			'Combine with trend filter to avoid counter-trend disasters',
			'Higher win rate but winners typically smaller than losers',
			'Requires strict risk management for tail risk'
		],
		relatedTerms: ['rsi', 'cci', 'bollinger_bands', 'trend_following'],
		references: [
			'Poterba, J. & Summers, L. (1988). "Mean Reversion in Stock Prices." Journal of Financial Economics.'
		]
	},

	trend_following: {
		id: 'trend_following',
		term: 'Trend Following',
		shortDescription: 'Strategy that identifies and rides sustained price movements.',
		category: 'strategy',
		fullDescription: `Trend Following strategies aim to identify when price is moving in a sustained direction and "go with the flow." Rather than predicting reversals, these strategies enter after a trend is established and exit when it ends.

Common trend following indicators:
- **Moving Average Crossovers**: Buy when fast MA crosses above slow MA
- **Channel Breakouts**: Buy when price breaks above recent highs
- **ADX** (Average Directional Index): Measures trend strength

Trend following typically has lower win rates (30-45%) but larger winners than losers. Profitable because occasional big trends more than compensate for frequent small losses.`,
		proTips: [
			'Best in markets that exhibit momentum (crypto, commodities)',
			'Expect many small losses and occasional big wins',
			'Psychologically difficult - many losing trades before a winner',
			'Requires patience and discipline to hold winning trades',
			'Performs poorly in choppy, mean-reverting markets'
		],
		relatedTerms: ['moving_average', 'breakout', 'momentum', 'mean_reversion'],
		references: [
			'Hurst, B. et al. (2017). "A Century of Evidence on Trend-Following Investing." AQR.'
		]
	},

	// ═══════════════════════════════════════════════════════════════════════════
	// RISK MANAGEMENT
	// ═══════════════════════════════════════════════════════════════════════════

	position_sizing: {
		id: 'position_sizing',
		term: 'Position Sizing',
		shortDescription: 'Determining how much capital to allocate per trade.',
		category: 'risk',
		fullDescription: `Position sizing determines what percentage of capital to risk on each trade. It's often more important than entry/exit signals because it directly controls risk of ruin and drawdown magnitude.

Common methods:
- **Fixed Percentage**: Risk same % of capital per trade (e.g., 2%)
- **Fixed Dollar**: Risk same $ amount per trade
- **Volatility-Based**: Adjust size inversely to asset volatility
- **Kelly Criterion**: Optimal sizing based on edge and win rate

Conservative position sizing (1-2% risk per trade) protects against ruin but may limit returns. Aggressive sizing (5%+) can compound faster but risks catastrophic drawdowns.`,
		formula: {
			latex: 'Position\\ Size = \\frac{Account \\times Risk\\%}{Stop\\ Distance}',
			plain: 'Position Size = (Account × Risk%) / (Entry - Stop)',
			variables: {
				'Account': 'Total capital available',
				'Risk%': 'Maximum percentage willing to lose per trade (typically 1-2%)',
				'Stop Distance': 'Distance from entry to stop-loss in price terms'
			}
		},
		proTips: [
			'Never risk more than 2% per trade when starting out',
			'Account for correlation - similar positions compound risk',
			'Reduce size during drawdowns, increase during winning streaks',
			'Backtest with your intended position sizing, not 100% capital',
			'Kelly Criterion is theoretically optimal but practically too aggressive'
		],
		relatedTerms: ['risk_of_ruin', 'kelly_criterion', 'drawdown', 'leverage'],
		references: [
			'Tharp, V.K. (1998). "Trade Your Way to Financial Freedom." McGraw-Hill.',
			'Vince, R. (1990). "Portfolio Management Formulas." Wiley.'
		]
	},

	stop_loss: {
		id: 'stop_loss',
		term: 'Stop-Loss',
		shortDescription: 'Order to exit position when price moves against you by specified amount.',
		category: 'risk',
		fullDescription: `A stop-loss is a predetermined exit point that limits losses on a trade. When price reaches the stop-loss level, the position is closed automatically to prevent further loss.

Types of stop-losses:
- **Fixed**: Set dollar or percentage amount from entry
- **ATR-Based**: Distance based on recent volatility
- **Technical**: Below support or recent swing low
- **Trailing**: Moves with price, locking in profits

Stop-losses are essential for risk management but can also reduce returns if set too tight (stopped out of winning trades) or if the market gaps through the stop level.`,
		proTips: [
			'Never trade without a stop-loss - hope is not a strategy',
			'Set stops at technical levels, not arbitrary numbers',
			'Account for spread and slippage in stop placement',
			'Wider stops need smaller position size to maintain risk',
			'Trailing stops can capture large trends while protecting profit'
		],
		relatedTerms: ['position_sizing', 'take_profit', 'risk_reward_ratio', 'slippage'],
		references: []
	},

	slippage: {
		id: 'slippage',
		term: 'Slippage',
		shortDescription: 'Difference between expected and actual execution price.',
		category: 'risk',
		fullDescription: `Slippage is the difference between the price you expected to trade at and the price you actually received. It occurs due to:

1. **Market Movement**: Price moves between order submission and execution
2. **Liquidity**: Large orders move the market
3. **Gaps**: Price jumps past your intended level
4. **Exchange Latency**: Delays in order processing

Slippage is always a cost - you rarely slip in your favor. In backtests, slippage is estimated; in live trading, it's real and often larger than expected, especially for less liquid markets or larger positions.`,
		formula: {
			latex: 'Slippage = |Executed\\ Price - Expected\\ Price|',
			plain: 'Slippage = |Executed Price - Expected Price|',
			variables: {
				'Executed Price': 'Actual fill price received',
				'Expected Price': 'Price when order was submitted'
			}
		},
		proTips: [
			'Use 5-10 basis points slippage estimate for backtests',
			'More volatile assets have higher slippage',
			'Use limit orders to control slippage (but risk non-execution)',
			'Trade during high liquidity periods (market hours)',
			'Smaller position sizes experience less slippage'
		],
		relatedTerms: ['trading_costs', 'liquidity', 'market_impact', 'fees'],
		references: []
	},

	fees: {
		id: 'fees',
		term: 'Trading Fees',
		shortDescription: 'Exchange and broker charges per trade.',
		category: 'risk',
		fullDescription: `Trading fees are charges applied by exchanges and brokers for executing trades. They directly reduce strategy profitability and can make otherwise profitable strategies unprofitable.

Types of fees:
- **Maker Fees**: Charged for adding liquidity (limit orders that don't immediately fill)
- **Taker Fees**: Charged for removing liquidity (market orders, limit orders that immediately fill)
- **Spread**: Difference between bid and ask prices
- **Funding Fees**: For perpetual futures, paid to hold positions

Crypto exchanges typically charge 0.1% (10 bps) per trade, meaning a round-trip (entry + exit) costs 0.2%. At 200 trades per year, this is 40% annual cost!`,
		formula: {
			latex: 'Round\\ Trip\\ Cost = 2 \\times (Fee\\ Rate + Slippage)',
			plain: 'Round Trip Cost = 2 × (Fee Rate + Slippage)',
			variables: {
				'Fee Rate': 'Per-trade fee percentage (e.g., 0.1%)',
				'Slippage': 'Execution slippage estimate (e.g., 0.05%)'
			}
		},
		proTips: [
			'Always backtest with realistic fee estimates (10 bps minimum)',
			'Use maker orders for lower fees when timing is not critical',
			'Higher timeframes = fewer trades = lower total fee impact',
			'Some exchanges offer fee discounts for volume or native token holding',
			'Fees compound: 100 trades at 0.2% round-trip = 18% annual drag'
		],
		relatedTerms: ['slippage', 'trading_costs', 'maker_taker', 'spread'],
		references: []
	},

	exchange: {
		id: 'exchange',
		term: 'Exchange',
		shortDescription: 'Select the broker/exchange used for execution and data routing.',
		category: 'strategy',
		fullDescription: 'This selects the active execution venue. Your credentials, fee model, and order behavior depend on the selected exchange.'
	},
	wallet_address: {
		id: 'wallet_address',
		term: 'Wallet Address',
		shortDescription: 'Public address used to identify your Hyperliquid account.',
		category: 'data',
		fullDescription: 'This is your public wallet/account identifier. It is safe to share in logs, but must match the account tied to your trading key.'
	},
	private_key: {
		id: 'private_key',
		term: 'Private Key',
		shortDescription: 'Secret credential used to sign orders.',
		category: 'risk',
		fullDescription: 'This key authorizes real actions. Never paste it into chats, screenshots, or untrusted tools. Rotate it if exposure is suspected.'
	},
	api_key: {
		id: 'api_key',
		term: 'API Key',
		shortDescription: 'Public identifier for API authentication.',
		category: 'data',
		fullDescription: 'The API key identifies your application or account. It is paired with an API secret for authenticated exchange requests.'
	},
	api_secret: {
		id: 'api_secret',
		term: 'API Secret',
		shortDescription: 'Secret used with API key to authenticate signed requests.',
		category: 'risk',
		fullDescription: 'Treat this like a password. If leaked, revoke and regenerate immediately.'
	},
	testnet_mode: {
		id: 'testnet_mode',
		term: 'Testnet Mode',
		shortDescription: 'Route orders to a simulated exchange environment.',
		category: 'strategy',
		fullDescription: 'Use testnet for validation and dry runs. It avoids real capital risk but may not match live liquidity and slippage exactly.'
	},
	initial_capital: {
		id: 'initial_capital',
		term: 'Initial Capital',
		shortDescription: 'Capital baseline for backtests, simulations, and legacy risk conversions.',
		category: 'risk',
		fullDescription: 'This value anchors backtest and simulation sizing, reported returns, and legacy USD-to-percent risk conversions. It does not change the standardized per-strategy paper sandbox, which uses a fixed $10,000 benchmark.',
		interpretations: [
			{ range: '$1,000 - $100,000', label: 'Typical', color: 'green', description: 'Good starting range for paper/live validation.' },
			{ range: '< $1,000', label: 'Small', color: 'yellow', description: 'Useful for testing, but sizing/noise can be distorted.' },
			{ range: '> $100,000', label: 'Large', color: 'yellow', description: 'Use only if your real deployable capital is this high.' }
		],
		proTips: [
			'Recommended: match simulation capital to the scenario you are evaluating',
			'Paper strategy comparisons stay normalized to the fixed $10,000 sandbox'
		]
	},
	position_size_limit: {
		id: 'position_size_limit',
		term: 'Max Position Size',
		shortDescription: 'Upper cap on capital allocation per position.',
		category: 'risk',
		fullDescription: 'Limits concentration risk from any single trade. Lower values reduce blow-up risk at the cost of slower compounding.',
		interpretations: [
			{ range: '1% - 5%', label: 'Conservative', color: 'green', description: 'Best for most live systems and volatile markets.' },
			{ range: '5% - 10%', label: 'Balanced', color: 'yellow', description: 'Acceptable with strong risk controls.' },
			{ range: '> 10%', label: 'Aggressive', color: 'red', description: 'Concentration risk rises quickly.' }
		],
		proTips: ['Recommended default: 2% - 6% per position']
	},
	daily_loss_limit: {
		id: 'daily_loss_limit',
		term: 'Max Daily Loss',
		shortDescription: 'Daily stop-loss for total account drawdown.',
		category: 'risk',
		fullDescription: 'When reached, new trading should halt for the day. This protects against regime shifts and cascading losses.',
		interpretations: [
			{ range: '1% - 3% of capital', label: 'Recommended', color: 'green', description: 'Strong balance between protection and continuity.' },
			{ range: '3% - 5% of capital', label: 'Aggressive', color: 'yellow', description: 'Higher tolerance for intraday drawdowns.' },
			{ range: '> 5% of capital', label: 'High Risk', color: 'red', description: 'Large single-day damage possible.' }
		],
		proTips: ['Set this as a dollar amount based on your initial capital']
	},
	concurrent_positions: {
		id: 'concurrent_positions',
		term: 'Max Concurrent Positions',
		shortDescription: 'Maximum number of open positions at once.',
		category: 'risk',
		fullDescription: 'Caps portfolio complexity and correlated exposure. Increase only when you have diversified signals and enough liquidity.',
		interpretations: [
			{ range: '1 - 3', label: 'Recommended', color: 'green', description: 'Keeps risk transparent and easier to control.' },
			{ range: '4 - 6', label: 'Moderate', color: 'yellow', description: 'Needs stronger correlation controls.' },
			{ range: '> 6', label: 'Complex', color: 'red', description: 'Execution and correlation risk can compound.' }
		]
	},
	cooldown_after_loss: {
		id: 'cooldown_after_loss',
		term: 'Cooldown After Loss',
		shortDescription: 'Waiting period before new entries after a loss.',
		category: 'risk',
		fullDescription: 'Helps reduce revenge-trading behavior and repeated entries in unstable conditions.',
		interpretations: [
			{ range: '0.5 - 4 hours', label: 'Recommended', color: 'green', description: 'Useful buffer without over-throttling strategy flow.' },
			{ range: '0 hours', label: 'No Cooldown', color: 'yellow', description: 'Maximum throughput but less behavioral protection.' },
			{ range: '> 8 hours', label: 'Strict', color: 'yellow', description: 'Reduces overtrading, but can miss valid setups.' }
		]
	},
	strategy_name: {
		id: 'strategy_name',
		term: 'Strategy Name',
		shortDescription: 'Primary strategy identifier used by execution.',
		category: 'strategy',
		fullDescription: 'This selects the named strategy that live/paper trading will run by default.'
	},
	trading_symbol: {
		id: 'trading_symbol',
		term: 'Trading Symbol',
		shortDescription: 'Instrument market pair to trade.',
		category: 'strategy',
		fullDescription: 'Defines the asset pair for execution and backtesting. Must match available market symbols on the selected exchange.'
	},
	trading_timeframe: {
		id: 'trading_timeframe',
		term: 'Trading Timeframe',
		shortDescription: 'Bar interval used to evaluate signals.',
		category: 'strategy',
		fullDescription: 'Lower timeframes react faster but are noisier and costlier. Higher timeframes are smoother but slower.'
	},
	webhook_url: {
		id: 'webhook_url',
		term: 'Webhook URL',
		shortDescription: 'Destination endpoint for notifications.',
		category: 'data',
		fullDescription: 'Forven posts event messages to this endpoint. Keep it private to avoid alert spoofing.'
	},
	notification_level: {
		id: 'notification_level',
		term: 'Notification Level',
		shortDescription: 'Controls which events generate alerts.',
		category: 'strategy',
		fullDescription: 'Choose between full trade stream, summaries, alerts-only, or silent mode to balance noise vs. visibility.',
		interpretations: [
			{ range: 'all', label: 'Recommended (Paper)', color: 'green', description: 'Best for debugging and validating execution flow.' },
			{ range: 'alerts', label: 'Recommended (Live)', color: 'green', description: 'Keeps signal high while preserving critical alerts.' },
			{ range: 'none', label: 'Minimal', color: 'yellow', description: 'Low noise but low observability.' }
		]
	},
	data_refresh_interval: {
		id: 'data_refresh_interval',
		term: 'Data Refresh Interval',
		shortDescription: 'How often market/account data is polled.',
		category: 'data',
		fullDescription: 'Short intervals improve responsiveness but increase API load. Keep within exchange rate limits.',
		interpretations: [
			{ range: '30 - 120 seconds', label: 'Recommended', color: 'green', description: 'Responsive enough without excessive API pressure.' },
			{ range: '10 - 30 seconds', label: 'Fast', color: 'yellow', description: 'Higher load; monitor rate limits carefully.' },
			{ range: '> 180 seconds', label: 'Slow', color: 'yellow', description: 'Lower load but stale monitoring data.' }
		]
	},
	auto_restart_on_crash: {
		id: 'auto_restart_on_crash',
		term: 'Auto-Restart on Crash',
		shortDescription: 'Automatically restarts the bot process after failure.',
		category: 'risk',
		fullDescription: 'Improves uptime after transient failures. Keep logging/alerts enabled so crash loops are visible and not silently repeated.'
	},
	maintenance_start_utc: {
		id: 'maintenance_start_utc',
		term: 'Maintenance Start (UTC)',
		shortDescription: 'Start hour for scheduled no-trade window.',
		category: 'strategy',
		fullDescription: 'Use this to pause trading during known low-liquidity or operational windows.'
	},
	maintenance_end_utc: {
		id: 'maintenance_end_utc',
		term: 'Maintenance End (UTC)',
		shortDescription: 'End hour for scheduled no-trade window.',
		category: 'strategy',
		fullDescription: 'Trading resumes after this hour. Keep start/end aligned to your exchange and timezone assumptions.'
	},
	health_checks: {
		id: 'health_checks',
		term: 'Health Checks',
		shortDescription: 'Automated strategy validation on a schedule.',
		category: 'walkforward',
		fullDescription: 'Runs recurring tests to detect degradation before capital is impacted.'
	},
	rolling_backtest_days_setting: {
		id: 'rolling_backtest_days_setting',
		term: 'Rolling Backtest Days',
		shortDescription: 'Lookback window for recurring re-tests.',
		category: 'walkforward',
		fullDescription: 'Longer windows are more stable but slower to react; shorter windows adapt faster but are noisier.',
		interpretations: [
			{ range: '14 - 60 days', label: 'Recommended', color: 'green', description: 'Good balance of stability and adaptability.' },
			{ range: '7 - 14 days', label: 'Reactive', color: 'yellow', description: 'Faster response to change, higher false alarms.' },
			{ range: '> 60 days', label: 'Stable', color: 'yellow', description: 'Smoother signal, slower detection of decay.' }
		]
	},
	walkforward_months_setting: {
		id: 'walkforward_months_setting',
		term: 'Walk-Forward Months',
		shortDescription: 'History span used for walk-forward validation.',
		category: 'walkforward',
		fullDescription: 'Defines how much data is used to test strategy robustness across time.',
		interpretations: [
			{ range: '6 - 18 months', label: 'Recommended', color: 'green', description: 'Solid for most crypto strategy validation loops.' },
			{ range: '3 - 6 months', label: 'Short', color: 'yellow', description: 'Faster runs but weaker statistical confidence.' },
			{ range: '> 18 months', label: 'Long', color: 'yellow', description: 'More robust but heavier compute and slower feedback.' }
		]
	},
	walkforward_folds_setting: {
		id: 'walkforward_folds_setting',
		term: 'Walk-Forward Folds',
		shortDescription: 'Number of train/test splits in walk-forward.',
		category: 'walkforward',
		fullDescription: 'More folds improve robustness confidence but increase compute cost.',
		interpretations: [
			{ range: '4 - 8 folds', label: 'Recommended', color: 'green', description: 'Good confidence without excessive runtime.' },
			{ range: '3 folds', label: 'Minimum', color: 'yellow', description: 'Lower confidence; acceptable for quick checks.' },
			{ range: '> 8 folds', label: 'Heavy', color: 'yellow', description: 'Higher confidence but much slower execution.' }
		]
	},
	degradation_alert_pct: {
		id: 'degradation_alert_pct',
		term: 'Degradation Alert %',
		shortDescription: 'Threshold drop that triggers health alerts.',
		category: 'risk',
		fullDescription: 'If recent performance falls by this amount, Forven flags potential regime drift or edge decay.',
		interpretations: [
			{ range: '10% - 30%', label: 'Recommended', color: 'green', description: 'Balanced sensitivity for most strategies.' },
			{ range: '< 10%', label: 'Sensitive', color: 'yellow', description: 'More alerts; catches issues earlier.' },
			{ range: '> 30%', label: 'Coarse', color: 'yellow', description: 'Fewer alerts; may react too late.' }
		]
	},
	regime_detection: {
		id: 'regime_detection',
		term: 'Regime Detection',
		shortDescription: 'Detects when market behavior shifts.',
		category: 'strategy',
		fullDescription: 'Flags transitions like trend-to-range or volatility expansion so you can adjust exposure or strategy mix.'
	},
	exchange_connection_settings: {
		id: 'exchange_connection_settings',
		term: 'Exchange Connection',
		shortDescription: 'Primary connectivity settings for exchange execution and account access.',
		category: 'data',
		fullDescription: 'Use this section to choose exchange, configure credentials, and select testnet vs live connectivity.',
		proTips: [
			'Recommended: keep testnet enabled until order and risk flows are fully validated',
			'Verify wallet/key permissions after any credential change'
		]
	},
	risk_management_settings: {
		id: 'risk_management_settings',
		term: 'Risk Management',
		shortDescription: 'Portfolio-level guardrails for position sizing and loss containment.',
		category: 'risk',
		fullDescription: 'These controls cap downside and reduce concentration risk. Keep limits conservative before scaling throughput.',
		proTips: [
			'Recommended baseline: max position 2-6%, daily loss 1-3%, max drawdown 10-25%',
			'Adjust limits only after reviewing recent volatility and strategy stability'
		]
	},
	active_strategy_settings: {
		id: 'active_strategy_settings',
		term: 'Active Strategy',
		shortDescription: 'Default strategy identity used by trading sessions.',
		category: 'strategy',
		fullDescription: 'Defines strategy name, symbol, and timeframe that paper/live execution references by default.',
		proTips: [
			'Recommended: use liquid symbols and tested timeframes (for example 15m, 1h, 4h)',
			'Keep names stable for cleaner audit and performance tracking'
		]
	},
	discord_notifications_settings: {
		id: 'discord_notifications_settings',
		term: 'Discord Notifications',
		shortDescription: 'Outbound alerting channel for trade and system events.',
		category: 'data',
		fullDescription: 'Controls webhook alerts for entries, exits, summaries, health reports, and errors.',
		proTips: [
			'Recommended level: all in paper mode, alerts in live mode',
			'Always keep error notifications enabled in production'
		]
	},
	continuous_testing_settings: {
		id: 'continuous_testing_settings',
		term: 'Continuous Testing',
		shortDescription: 'Recurring validation loop for strategy health and degradation checks.',
		category: 'walkforward',
		fullDescription: 'Automates rolling backtests and walk-forward checks to detect edge decay and regime drift.',
		proTips: [
			'Recommended ranges: rolling 14-60 days, walk-forward 6-18 months, folds 4-8',
			'Set degradation alert threshold around 10-30% for balanced sensitivity'
		]
	},
	forven_service_status: {
		id: 'forven_service_status',
		term: 'Forven Service Status',
		shortDescription: 'Live health panel for API, daemon, trading state, and bridge connectivity.',
		category: 'risk',
		fullDescription: 'Use this panel to confirm runtime readiness before changing settings or enabling live mode.',
		proTips: [
			'Recommended: API connected, daemon running, exchange feed receiving before active trading',
			'If status is offline, resolve connectivity before queue processing'
		]
	},
	factory_reset_settings: {
		id: 'factory_reset_settings',
		term: 'Factory Reset',
		shortDescription: 'Selective data wipe with keep-list confirmation controls.',
		category: 'risk',
		fullDescription: 'Unchecked categories are wiped. Use this for clean-state resets while preserving chosen categories such as credentials.',
		proTips: [
			'Recommended keep set: credentials (and optionally settings) unless intentionally rotating everything',
			'Type-check confirmation carefully; reset actions are permanent'
		]
	},
	trading_mode_setting: {
		id: 'trading_mode_setting',
		term: 'Trading Mode',
		shortDescription: 'Select whether orders are simulated or sent to real markets.',
		category: 'risk',
		fullDescription: 'Paper mode is recommended for validation and debugging. Live mode should be enabled only after stable paper performance and risk controls are verified.',
		interpretations: [
			{ range: 'paper', label: 'Recommended', color: 'green', description: 'Best default while tuning strategy and infrastructure.' },
			{ range: 'live', label: 'Production', color: 'yellow', description: 'Use only when guardrails and credentials are fully validated.' }
		]
	},
	data_source_api_keys: {
		id: 'data_source_api_keys',
		term: 'Data Source API Keys',
		shortDescription: 'Credentials used for non-exchange market and macro data feeds.',
		category: 'data',
		fullDescription: 'Configure API keys for sources like Tiingo, FRED, CoinGecko, Polygon, and Alpaca. Keep only providers you actively use.',
		proTips: [
			'Recommended: keep 1-3 active providers to reduce operational overhead',
			'Rotate keys at least every 90-180 days'
		]
	},
	api_key_entry: {
		id: 'api_key_entry',
		term: 'API Key Entry',
		shortDescription: 'Add or rotate a provider API key.',
		category: 'data',
		fullDescription: 'Use this form to set a new key value for a selected provider. Existing keys are replaced when a new value is saved.',
		proTips: [
			'Recommended rotation window: every 90-180 days',
			'Save one key at a time and run a Test right after each update'
		]
	},
	ai_provider_authentication: {
		id: 'ai_provider_authentication',
		term: 'AI Provider Authentication',
		shortDescription: 'Manage OAuth and token credentials for model providers.',
		category: 'strategy',
		fullDescription: 'This section controls access to model providers used by agents and pipeline tasks. Keep credentials current to avoid queue stalls.',
		proTips: [
			'Recommended: keep at least one primary and one fallback provider configured',
			'Refresh and validate provider auth weekly in active environments'
		]
	},
	agent_model_visibility: {
		id: 'agent_model_visibility',
		term: 'Agent Model Visibility',
		shortDescription: 'Controls which provider/model pairs appear in agent model selectors.',
		category: 'optimization',
		fullDescription: 'Restricting visible models improves consistency and prevents accidental model drift across agents.',
		interpretations: [
			{ range: '3 - 8 models', label: 'Recommended', color: 'green', description: 'Enough optionality without configuration sprawl.' },
			{ range: '1 - 2 models', label: 'Strict', color: 'yellow', description: 'Very stable but limited fallback options.' },
			{ range: '> 10 models', label: 'Broad', color: 'yellow', description: 'Flexible but easier to misconfigure.' }
		]
	},
	oauth_quick_link: {
		id: 'oauth_quick_link',
		term: 'OAuth Quick Link',
		shortDescription: 'Generates authorization URL/device flow details for provider login.',
		category: 'strategy',
		fullDescription: 'Use this to initiate an OAuth grant flow and authorize Forven against the provider account.',
		proTips: [
			'Complete generated OAuth links within 2-10 minutes to avoid state expiry',
			'Recommended: finish setup in one session, then test immediately'
		]
	},
	oauth_authorization_code: {
		id: 'oauth_authorization_code',
		term: 'Authorization Code',
		shortDescription: 'Temporary code or callback URL returned by OAuth provider.',
		category: 'strategy',
		fullDescription: 'Paste the provider callback URL or direct code to complete the authorization-code flow.',
		proTips: [
			'Most providers issue codes valid for about 1-10 minutes',
			'Recommended: submit code immediately after generation'
		]
	},
	oauth_access_token: {
		id: 'oauth_access_token',
		term: 'Access Token',
		shortDescription: 'Short-lived bearer token used for authenticated provider API calls.',
		category: 'risk',
		fullDescription: 'Access tokens authorize live API requests. Expired or invalid values will break provider-backed model calls.',
		proTips: [
			'Recommended lifetime target: 15 minutes to 24 hours, provider dependent',
			'Rotate immediately if token leakage is suspected'
		]
	},
	oauth_refresh_token: {
		id: 'oauth_refresh_token',
		term: 'Refresh Token',
		shortDescription: 'Longer-lived token used to mint new access tokens.',
		category: 'risk',
		fullDescription: 'Refresh tokens keep provider auth persistent without repeated manual logins.',
		proTips: [
			'Recommended lifetime target: 30-180 days, provider dependent',
			'Store only when provider supports secure rotation/revocation'
		]
	},
	oauth_expires_at: {
		id: 'oauth_expires_at',
		term: 'Token Expiry Timestamp',
		shortDescription: 'Optional explicit expiration timestamp for the current token set.',
		category: 'risk',
		fullDescription: 'Supports either ISO-8601 UTC timestamps or epoch milliseconds, and helps Forven determine when credentials should be renewed.',
		proTips: [
			'Recommended format: ISO-8601 UTC (for example 2026-01-01T00:00:00Z)',
			'Keep at least a 5-15 minute safety buffer before hard expiry'
		]
	},
	scheduler_jobs_settings: {
		id: 'scheduler_jobs_settings',
		term: 'Scheduler Jobs',
		shortDescription: 'Controls timed execution of core pipeline and ops jobs.',
		category: 'optimization',
		fullDescription: 'Each job controls a recurring process. Tune cadence based on compute headroom, queue depth, and desired pipeline velocity.',
		proTips: [
			'Recommended: adjust one job at a time and monitor for at least 1-2 cycles',
			'Keep heavy jobs staggered to avoid synchronized load spikes'
		]
	},
	scheduler_job_enabled: {
		id: 'scheduler_job_enabled',
		term: 'Job Enabled',
		shortDescription: 'Toggles whether a scheduler job is active.',
		category: 'optimization',
		fullDescription: 'Disabled jobs are skipped by the scheduler loop until re-enabled.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Keep core pipeline jobs active in normal operation.' },
			{ range: 'disabled', label: 'Maintenance', color: 'yellow', description: 'Use for troubleshooting or controlled pause windows.' }
		]
	},
	scheduler_job_type: {
		id: 'scheduler_job_type',
		term: 'Schedule Type',
		shortDescription: 'Defines whether cadence is cron-based or fixed-millisecond interval.',
		category: 'optimization',
		fullDescription: 'Cron is calendar/time-of-day oriented. Interval is fixed duration between runs.',
		interpretations: [
			{ range: 'cron', label: 'Recommended', color: 'green', description: 'Best for human-readable, wall-clock schedules.' },
			{ range: 'interval', label: 'High-Precision', color: 'yellow', description: 'Useful for tight periodic loops in milliseconds.' }
		]
	},
	scheduler_job_expression: {
		id: 'scheduler_job_expression',
		term: 'Schedule Expression',
		shortDescription: 'Cron string or millisecond interval value for a job.',
		category: 'optimization',
		fullDescription: 'Use 5-field cron (minute hour day month weekday) or an interval in milliseconds.',
		interpretations: [
			{ range: 'cron: 5 fields', label: 'Recommended', color: 'green', description: 'Example: 0 * * * * (hourly).' },
			{ range: 'interval: 60000-3600000 ms', label: 'Common', color: 'green', description: '1 minute to 1 hour interval ranges are typical.' },
			{ range: '< 10000 ms', label: 'Aggressive', color: 'yellow', description: 'High frequency can overload jobs or APIs.' }
		]
	},
	agents_settings: {
		id: 'agents_settings',
		term: 'Agents Settings',
		shortDescription: 'Configure agent identity, model routing, schedules, and docs.',
		category: 'strategy',
		fullDescription: 'This section controls autonomous workers used throughout the strategy pipeline and operational task queues.',
		proTips: [
			'Recommended: keep role definitions explicit and non-overlapping',
			'Review agent configs weekly when tuning throughput'
		]
	},
	agent_list: {
		id: 'agent_list',
		term: 'Agent List',
		shortDescription: 'Inventory of configured agents and their run state.',
		category: 'strategy',
		fullDescription: 'Select an agent from this list to edit model, schedule, enablement, and instruction settings.',
		interpretations: [
			{ range: '3 - 10 agents', label: 'Recommended', color: 'green', description: 'Sufficient separation of roles without heavy management overhead.' },
			{ range: '> 15 agents', label: 'Complex', color: 'yellow', description: 'Higher operational burden and coordination complexity.' }
		]
	},
	agent_configuration: {
		id: 'agent_configuration',
		term: 'Agent Configuration',
		shortDescription: 'Editable runtime settings for one selected agent.',
		category: 'strategy',
		fullDescription: 'Updates agent metadata, model binding, schedule behavior, and instruction prompts used during task execution.',
		proTips: [
			'Recommended: change one field group at a time and observe queue behavior',
			'Always validate model/provider combinations after edits'
		]
	},
	agent_name_setting: {
		id: 'agent_name_setting',
		term: 'Agent Name',
		shortDescription: 'Human-readable display name for the agent.',
		category: 'strategy',
		fullDescription: 'Used in UI tables, queue records, and audit trails.',
		proTips: ['Recommended length: 3-40 characters, stable and role-descriptive']
	},
	agent_role_setting: {
		id: 'agent_role_setting',
		term: 'Agent Role',
		shortDescription: 'Functional responsibility label for the agent.',
		category: 'strategy',
		fullDescription: 'Role names should map clearly to a pipeline stage or operational domain.',
		proTips: ['Recommended length: 3-60 characters, use concise lowercase role labels']
	},
	agent_model_provider_setting: {
		id: 'agent_model_provider_setting',
		term: 'Model Provider',
		shortDescription: 'Provider namespace used for model selection and routing.',
		category: 'strategy',
		fullDescription: 'Must match an authenticated provider key recognized by Forven.',
		proTips: ['Recommended: choose providers with both primary and fallback models configured']
	},
	agent_model_id_setting: {
		id: 'agent_model_id_setting',
		term: 'Model ID',
		shortDescription: 'Exact model identifier used by the selected provider.',
		category: 'strategy',
		fullDescription: 'Controls model capability, latency, and token cost profile for this agent.',
		proTips: [
			'Recommended: keep 1 primary model per agent and test fallback in policy',
			'Revalidate prompt behavior when switching model family'
		]
	},
	agent_schedule_type_setting: {
		id: 'agent_schedule_type_setting',
		term: 'Agent Schedule Type',
		shortDescription: 'Determines whether the agent runs on cron or interval cadence.',
		category: 'optimization',
		fullDescription: 'Choose the schedule style that best fits task regularity and load characteristics.',
		interpretations: [
			{ range: 'cron', label: 'Recommended', color: 'green', description: 'Best for predictable recurring windows.' },
			{ range: 'interval', label: 'Continuous', color: 'yellow', description: 'Useful for steady queue draining loops.' }
		]
	},
	agent_schedule_expr_setting: {
		id: 'agent_schedule_expr_setting',
		term: 'Agent Schedule Expression',
		shortDescription: 'Cron expression or interval milliseconds for this agent.',
		category: 'optimization',
		fullDescription: 'Controls how frequently the agent wakes to process work.',
		interpretations: [
			{ range: 'cron: every 5-60 min', label: 'Recommended', color: 'green', description: 'Common cadence for stable production loops.' },
			{ range: 'interval: 30000-300000 ms', label: 'Recommended', color: 'green', description: '30s to 5m for responsive queue handling.' },
			{ range: '< 10000 ms', label: 'Aggressive', color: 'yellow', description: 'Can create noisy rapid polling and higher load.' }
		]
	},
	agent_enabled_setting: {
		id: 'agent_enabled_setting',
		term: 'Agent Enabled',
		shortDescription: 'Master switch for whether this agent can claim and run tasks.',
		category: 'risk',
		fullDescription: 'Disabling an agent pauses task execution for that role without deleting configuration.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Normal production and testing mode.' },
			{ range: 'disabled', label: 'Pause', color: 'yellow', description: 'Use during maintenance or incident isolation.' }
		]
	},
	agent_instructions_setting: {
		id: 'agent_instructions_setting',
		term: 'Agent Instructions',
		shortDescription: 'Prompt directives that shape the agent behavior and output format.',
		category: 'strategy',
		fullDescription: 'Instruction quality heavily impacts consistency, tool usage, and error rate.',
		proTips: [
			'Recommended length: 100-1000 words, explicit and testable',
			'Prefer deterministic rules and concrete output constraints'
		]
	},
	agent_docs_setting: {
		id: 'agent_docs_setting',
		term: 'Agent Docs',
		shortDescription: 'Editable markdown source files that define persistent agent behavior.',
		category: 'strategy',
		fullDescription: 'SOUL.md and AGENTS.md act as long-lived behavior docs. Changes can alter policy, style, and execution behavior.',
		proTips: [
			'Recommended: version-control doc edits and test in paper mode first',
			'Restart or reload background workers after major instruction updates'
		]
	},
	notify_on_entry: {
		id: 'notify_on_entry',
		term: 'Notify on Trade Entry',
		shortDescription: 'Send a message whenever a new position is opened.',
		category: 'data',
		fullDescription: 'Useful for real-time visibility into strategy behavior and execution timing.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Keep enabled for paper and early live rollout.' },
			{ range: 'disabled', label: 'Low Noise', color: 'yellow', description: 'Reduces message volume but lowers observability.' }
		]
	},
	notify_on_exit: {
		id: 'notify_on_exit',
		term: 'Notify on Trade Exit',
		shortDescription: 'Send a message whenever a position is closed.',
		category: 'data',
		fullDescription: 'Critical for monitoring realized PnL and validating stop/target execution.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Keep enabled in both paper and live modes.' },
			{ range: 'disabled', label: 'Low Noise', color: 'yellow', description: 'Less noise, but you may miss important outcomes.' }
		]
	},
	notify_daily_summary: {
		id: 'notify_daily_summary',
		term: 'Daily P&L Summary',
		shortDescription: 'Send a once-per-day summary of performance.',
		category: 'data',
		fullDescription: 'Provides a low-noise operating view when you do not need per-trade notifications.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Good baseline observability for ongoing ops.' },
			{ range: 'disabled', label: 'Optional', color: 'yellow', description: 'Disable only if another reporting pipeline exists.' }
		]
	},
	notify_health_reports: {
		id: 'notify_health_reports',
		term: 'Strategy Health Reports',
		shortDescription: 'Send alerts from rolling backtests and validation checks.',
		category: 'risk',
		fullDescription: 'Helps catch degradation and regime drift before losses escalate.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Strongly recommended in live mode.' },
			{ range: 'disabled', label: 'Risky', color: 'red', description: 'Degradation can go unnoticed longer.' }
		]
	},
	notify_errors: {
		id: 'notify_errors',
		term: 'Errors and Warnings',
		shortDescription: 'Send operational failure and warning notifications.',
		category: 'risk',
		fullDescription: 'This is your early-warning channel for runtime issues, queue stalls, and failed jobs.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Keep always enabled in production.' },
			{ range: 'disabled', label: 'Not Recommended', color: 'red', description: 'Operational failures become harder to detect quickly.' }
		]
	},
	operations_settings: {
		id: 'operations_settings',
		term: 'Bot Operations Settings',
		shortDescription: 'Runtime controls for cadence, queue throughput, and recovery behavior.',
		category: 'optimization',
		fullDescription: 'These settings directly control how aggressively Forven generates work, how fast queues drain, how scanner execution is permitted and routed, and how quickly stale tasks are recovered.',
		proTips: [
			'Increase throughput gradually, then monitor scheduler errors and queue latency',
			'Use conservative values in live mode, aggressive values in paper mode',
			'When auto scheduler cadence control is enabled, managed job cadence comes from these settings rather than manual scheduler edits'
		]
	},
	scanner_execution_enabled: {
		id: 'scanner_execution_enabled',
		term: 'Scanner Execution Permission',
		shortDescription: 'Controls whether scanner execution runs are allowed to place trades.',
		category: 'strategy',
		fullDescription: 'This is the policy gate for scheduled and manual execution scans. When disabled, execution-requested scans still evaluate signals but are forced into signal-only-by-policy mode and must not place exchange orders.',
		interpretations: [
			{ range: 'enabled', label: 'Execution Allowed', color: 'green', description: 'Scanner execution runs may place trades when other trading gates also allow it.' },
			{ range: 'disabled', label: 'Signal-Only', color: 'yellow', description: 'Execution-requested scans still run, but they only produce signals and queue no exchange actions.' }
		]
	},
	throughput_auto_scheduler_control: {
		id: 'throughput_auto_scheduler_control',
		term: 'Auto Scheduler Cadence Control',
		shortDescription: 'Automatically sync scheduler intervals from throughput settings.',
		category: 'optimization',
		fullDescription: 'When enabled, ideation, coding, testing, paper graduation, scanner signal, and scanner execution jobs are re-timed using the interval settings below. Manual edits to those managed jobs are overwritten on settings save or scheduler tick.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Single source of truth for cadence tuning.' },
			{ range: 'disabled', label: 'Manual', color: 'yellow', description: 'Edit individual scheduler jobs directly.' }
		]
	},
	adaptive_pipeline_throughput_setting: {
		id: 'adaptive_pipeline_throughput_setting',
		term: 'Adaptive Pipeline Throughput',
		shortDescription: 'Dynamically scales testing throughput to clear backlog and accelerate promotion decisions.',
		category: 'optimization',
		fullDescription: 'When enabled, Forven adjusts testing-cycle throughput based on quick_screen/gauntlet backlog, cadence, and queue pressure. This helps move strong candidates forward quickly and purge weak candidates sooner.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Best for high-throughput strategy factories.' },
			{ range: 'disabled', label: 'Static', color: 'yellow', description: 'Uses fixed per-cycle limits only.' }
		]
	},
	ideation_interval_minutes_setting: {
		id: 'ideation_interval_minutes_setting',
		term: 'Strategy-Creation Check Interval (Minutes)',
		shortDescription: 'How often the scheduler checks whether to queue strategy-creation tasks.',
		category: 'optimization',
		fullDescription: 'Each check fills the free in-flight slots, up to the daily strategy-creation budget. The budget sets the volume; this interval only limits how fast it can be used (checks per day times tasks in flight).',
		interpretations: [
			{ range: '15-30 min', label: 'Recommended', color: 'green', description: 'Budgets up to about 90 tasks/day are reachable with 2 tasks in flight.' },
			{ range: '60-240 min', label: 'Slow', color: 'yellow', description: 'Fine for small budgets; a large budget may not be used up.' },
			{ range: '> 240 min', label: 'Trickle', color: 'yellow', description: 'A few checks per day, for hard-capped free models.' }
		]
	},
	coding_interval_minutes_setting: {
		id: 'coding_interval_minutes_setting',
		term: 'Coding Cadence (Minutes)',
		shortDescription: 'How often researching strategies are converted into coding tasks.',
		category: 'optimization',
		fullDescription: 'Controls developer-stage task generation frequency.',
		interpretations: [
			{ range: '1440 min (24h)', label: 'Recommended', color: 'green', description: 'Balanced daily cadence for most environments.' },
			{ range: '< 720 min', label: 'Aggressive', color: 'yellow', description: 'Can create fast queue growth if approvals are slow.' },
			{ range: '> 1440 min', label: 'Slow', color: 'yellow', description: 'Useful when prioritizing quality over speed.' }
		]
	},
	testing_interval_minutes_setting: {
		id: 'testing_interval_minutes_setting',
		term: 'Testing Cadence (Minutes)',
		shortDescription: 'How often backtesting assignment passes run.',
		category: 'optimization',
		fullDescription: 'Lower intervals create faster validation feedback loops for strategy progression.',
		interpretations: [
			{ range: '1440 min (24h)', label: 'Recommended', color: 'green', description: 'Good daily throughput without excessive compute churn.' },
			{ range: '< 720 min', label: 'Aggressive', color: 'yellow', description: 'Heavy backtest load; monitor capacity.' },
			{ range: '> 1440 min', label: 'Slow', color: 'yellow', description: 'Lower compute usage, slower promotion cycle.' }
		]
	},
	graduation_interval_minutes_setting: {
		id: 'graduation_interval_minutes_setting',
		term: 'Graduation Cadence (Minutes)',
		shortDescription: 'How often paper-trading graduation checks run.',
		category: 'optimization',
		fullDescription: 'Controls promotion review frequency from paper trading to deployment flow.',
		interpretations: [
			{ range: '1440 min (24h)', label: 'Recommended', color: 'green', description: 'Frequent enough for daily iteration, stable enough for review.' },
			{ range: '< 1440 min', label: 'Aggressive', color: 'yellow', description: 'Can produce noisy promotion pressure.' },
			{ range: '> 1440 min', label: 'Slow', color: 'yellow', description: 'Lower overhead, delayed graduation decisions.' }
		]
	},
	scanner_signal_interval_minutes_setting: {
		id: 'scanner_signal_interval_minutes_setting',
		term: 'Scanner Signal Cadence (Minutes)',
		shortDescription: 'How often signal-only scanner passes run.',
		category: 'optimization',
		fullDescription: 'Higher frequency improves idea/signal freshness but increases candle fetch and compute load.',
		interpretations: [
			{ range: '3 - 10 min', label: 'Recommended', color: 'green', description: 'Fast signal refresh without excessive backend pressure.' },
			{ range: '1 - 3 min', label: 'Aggressive', color: 'yellow', description: 'High throughput; monitor API limits and CPU.' },
			{ range: '> 15 min', label: 'Conservative', color: 'yellow', description: 'Lower load but less responsive signal updates.' }
		]
	},
	scanner_execution_interval_minutes_setting: {
		id: 'scanner_execution_interval_minutes_setting',
		term: 'Scanner Execution Cadence (Minutes)',
		shortDescription: 'How often scanner execution actions are applied.',
		category: 'optimization',
		fullDescription: 'Controls trade action frequency from scanner signals. Keep slower than signal cadence for cleaner batching.',
		interpretations: [
			{ range: '10 - 30 min', label: 'Recommended', color: 'green', description: 'Balanced execution throughput and control.' },
			{ range: '3 - 10 min', label: 'Aggressive', color: 'yellow', description: 'Faster action loop, can increase churn.' },
			{ range: '> 45 min', label: 'Slow', color: 'yellow', description: 'Low operational load, delayed entries/exits.' }
		]
	},
	daemon_candle_cache_refresh_seconds_setting: {
		id: 'daemon_candle_cache_refresh_seconds_setting',
		term: 'Daemon Candle Cache Refresh (Seconds)',
		shortDescription: 'How often daemon refreshes shared OHLCV candle cache for scanner workers.',
		category: 'optimization',
		fullDescription: 'Lower values improve indicator freshness for cache-first scanning but add exchange request load.',
		interpretations: [
			{ range: '60 - 180 sec', label: 'Recommended', color: 'green', description: 'Fresh enough for 1h systems with controlled API load.' },
			{ range: '15 - 60 sec', label: 'Aggressive', color: 'yellow', description: 'Very fresh cache, higher network pressure.' },
			{ range: '> 300 sec', label: 'Conservative', color: 'yellow', description: 'Lower load, slower cache freshness.' }
		]
	},
	scanner_allow_direct_market_fetch_setting: {
		id: 'scanner_allow_direct_market_fetch_setting',
		term: 'Scanner Direct Fetch Fallback',
		shortDescription: 'Allow scanner to call exchange directly when daemon candle cache is stale/unavailable.',
		category: 'risk',
		fullDescription: 'Keep enabled for resilience during daemon outages. Disable for strict data/exec separation and deterministic cache-only operation.',
		interpretations: [
			{ range: 'enabled', label: 'Resilient', color: 'green', description: 'Scanner keeps running if daemon cache is unavailable.' },
			{ range: 'disabled', label: 'Strict Separation', color: 'yellow', description: 'Scanner depends on daemon cache only.' }
		]
	},
	pipeline_assignments_per_cycle_setting: {
		id: 'pipeline_assignments_per_cycle_setting',
		term: 'Pipeline Assignments per Cycle',
		shortDescription: 'Maximum number of strategies assigned each coding/testing cycle.',
		category: 'optimization',
		fullDescription: 'Increases parallelism for pipeline progression.',
		interpretations: [
			{ range: '2 - 6', label: 'Recommended', color: 'green', description: 'Good throughput while keeping queue quality manageable.' },
			{ range: '1', label: 'Serialized', color: 'yellow', description: 'Safest but slowest throughput.' },
			{ range: '> 8', label: 'High Volume', color: 'yellow', description: 'Can overwhelm downstream agents or approvals.' }
		]
	},
	pipeline_drain_mode_setting: {
		id: 'pipeline_drain_mode_setting',
		term: 'Pipeline Drain Mode',
		shortDescription: 'Process all gauntlet steps for each strategy in a single cycle instead of one step per tick.',
		category: 'optimization',
		fullDescription: 'When enabled, the testing cycle drives each strategy through TF sweep, optimization, param application, confirmation backtest, and validation suite back-to-back within a single scheduler tick. This eliminates idle time between steps — a strategy can go from gauntlet entry to paper promotion in minutes instead of waiting 75+ minutes across multiple scheduler intervals. Uses only local compute, no API calls.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Maximize pipeline throughput — strategies advance through all steps without waiting.' },
			{ range: 'disabled', label: 'Legacy', color: 'yellow', description: 'One step per scheduler tick per strategy (slower, but lighter CPU load per cycle).' }
		]
	},
	pipeline_drain_max_seconds_setting: {
		id: 'pipeline_drain_max_seconds_setting',
		term: 'Drain Time Budget (Seconds)',
		shortDescription: 'Maximum time a single testing cycle can spend processing strategies in drain mode.',
		category: 'optimization',
		fullDescription: 'Safety valve to prevent a single testing cycle from monopolizing the scheduler. When the budget expires, the cycle yields so other jobs (scanning, ideation, etc.) can run. Any unfinished strategies resume in the next tick.',
		interpretations: [
			{ range: '120 - 300', label: 'Balanced', color: 'green', description: 'Good balance between throughput and scheduler responsiveness.' },
			{ range: '300 - 900', label: 'High Throughput', color: 'green', description: 'Lets more strategies complete per cycle at the cost of scheduler latency.' },
			{ range: '> 900', label: 'Maximum', color: 'yellow', description: 'Very long cycles — other jobs may be delayed.' }
		]
	},
	pipeline_target_clear_hours_setting: {
		id: 'pipeline_target_clear_hours_setting',
		term: 'Pipeline Clear Target (Hours)',
		shortDescription: 'Target horizon for clearing quick_screen + gauntlet backlog in adaptive mode.',
		category: 'optimization',
		fullDescription: 'Adaptive throughput uses this horizon to compute how many strategies to process per testing cycle. Lower values increase compute pressure for faster pipeline turnover.',
		interpretations: [
			{ range: '2 - 8 hours', label: 'Fast', color: 'green', description: 'Aggressive progression and backlog burn-down.' },
			{ range: '8 - 24 hours', label: 'Balanced', color: 'green', description: 'Strong throughput with moderate compute load.' },
			{ range: '> 24 hours', label: 'Conservative', color: 'yellow', description: 'Lower compute usage, slower progression.' }
		]
	},
	pipeline_gate_failure_archive_attempts_setting: {
		id: 'pipeline_gate_failure_archive_attempts_setting',
		term: 'Gate-Fail Archive Attempts',
		shortDescription: 'Number of gate failures before auto-archiving a candidate strategy.',
		category: 'optimization',
		fullDescription: 'Lower values purge weak strategies faster and reduce gauntlet noise. Higher values retain borderline candidates longer at the cost of queue growth.',
		interpretations: [
			{ range: '2 - 3', label: 'Fast Pruning', color: 'green', description: 'Recommended for high-throughput quality control.' },
			{ range: '4 - 6', label: 'Balanced', color: 'yellow', description: 'More retries before archive.' },
			{ range: '> 6', label: 'Lenient', color: 'yellow', description: 'Slowest garbage cleanup.' }
		]
	},
	task_stale_recovery_minutes_setting: {
		id: 'task_stale_recovery_minutes_setting',
		term: 'Task Stale Recovery (Minutes)',
		shortDescription: 'Timeout before running tasks are considered stale and recovered.',
		category: 'risk',
		fullDescription: 'Shorter values recover stuck work faster but can interrupt genuinely long tasks.',
		interpretations: [
			{ range: '5 - 20 min', label: 'Recommended', color: 'green', description: 'Fast recovery with low false-positive risk.' },
			{ range: '1 - 5 min', label: 'Aggressive', color: 'yellow', description: 'May requeue long-running valid tasks.' },
			{ range: '> 30 min', label: 'Lenient', color: 'yellow', description: 'Fewer interruptions, slower recovery from hangs.' }
		]
	},
	agent_task_claim_limit_setting: {
		id: 'agent_task_claim_limit_setting',
		term: 'Agent Queue Claim Limit',
		shortDescription: 'How many pending tasks an agent loop can claim per pass.',
		category: 'optimization',
		fullDescription: 'Higher values drain backlogs faster at the cost of larger burst load per agent.',
		interpretations: [
			{ range: '4 - 10', label: 'Recommended', color: 'green', description: 'Strong throughput for most queues.' },
			{ range: '1 - 3', label: 'Conservative', color: 'yellow', description: 'Predictable but slower backlog clearing.' },
			{ range: '> 10', label: 'Bursty', color: 'yellow', description: 'May saturate tools or model quotas per cycle.' }
		]
	},
	brain_task_claim_limit_setting: {
		id: 'brain_task_claim_limit_setting',
		term: 'Brain Queue Claim Limit',
		shortDescription: 'How many brain_invoke tasks are claimed per processing pass.',
		category: 'optimization',
		fullDescription: 'Controls how quickly brain review callbacks and orchestration messages are consumed.',
		interpretations: [
			{ range: '4 - 10', label: 'Recommended', color: 'green', description: 'Keeps orchestration responsive under load.' },
			{ range: '1 - 3', label: 'Conservative', color: 'yellow', description: 'Lower throughput, safer for constrained setups.' },
			{ range: '> 10', label: 'Aggressive', color: 'yellow', description: 'Can create response bursts and higher model load.' }
		]
	},
	code_strategy_requires_approval_setting: {
		id: 'code_strategy_requires_approval_setting',
		term: 'Code Strategy Approval Gate',
		shortDescription: 'Require CEO approval before code_strategy tasks can execute.',
		category: 'risk',
		fullDescription: 'Adds control and auditability for code-changing tasks but slows pipeline throughput.',
		interpretations: [
			{ range: 'disabled', label: 'Recommended (High Throughput)', color: 'green', description: 'Faster coding stage progression.' },
			{ range: 'enabled', label: 'Controlled', color: 'yellow', description: 'Better governance, slower execution.' }
		]
	},
	manual_queue_processor: {
		id: 'manual_queue_processor',
		term: 'Manual Queue Processor',
		shortDescription: 'Run one immediate queue processing pass without waiting for loops.',
		category: 'optimization',
		fullDescription: 'Useful for forcing recovery or draining backlog during testing and incident response.',
		proTips: ['Recommended use: on-demand diagnostics and backlog cleanup']
	},
	process_agent_tasks_setting: {
		id: 'process_agent_tasks_setting',
		term: 'Process Agent Tasks',
		shortDescription: 'Include agent task queue in manual processing pass.',
		category: 'optimization',
		fullDescription: 'Claims and executes pending tasks for enabled agents.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Use for normal queue draining.' },
			{ range: 'disabled', label: 'Skip', color: 'yellow', description: 'Use only when isolating issues.' }
		]
	},
	process_brain_tasks_setting: {
		id: 'process_brain_tasks_setting',
		term: 'Process Brain Tasks',
		shortDescription: 'Include brain_invoke queue in manual processing pass.',
		category: 'optimization',
		fullDescription: 'Consumes orchestration callbacks and brain review messages immediately.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Keeps orchestration responsive.' },
			{ range: 'disabled', label: 'Skip', color: 'yellow', description: 'Useful for staged debugging only.' }
		]
	},
	recover_stale_tasks_setting: {
		id: 'recover_stale_tasks_setting',
		term: 'Recover Stale Running Tasks',
		shortDescription: 'Requeue/mark stale running tasks before processing.',
		category: 'risk',
		fullDescription: 'Prevents deadlock from stuck `running` rows and restores forward progress.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Keep on for operational resilience.' },
			{ range: 'disabled', label: 'Manual', color: 'yellow', description: 'Only for targeted debugging workflows.' }
		]
	},
	stale_threshold_minutes_setting: {
		id: 'stale_threshold_minutes_setting',
		term: 'Stale Threshold (Minutes)',
		shortDescription: 'Threshold used by manual stale-recovery pass.',
		category: 'risk',
		fullDescription: 'Tasks running longer than this are considered stale for the manual recovery action.',
		interpretations: [
			{ range: '5 - 20 min', label: 'Recommended', color: 'green', description: 'Good balance for most workloads.' },
			{ range: '< 5 min', label: 'Aggressive', color: 'yellow', description: 'May interrupt valid long tasks.' },
			{ range: '> 30 min', label: 'Lenient', color: 'yellow', description: 'Slower stuck-task recovery.' }
		]
	},
	fail_agents_setting: {
		id: 'fail_agents_setting',
		term: 'Fail Specific Agents',
		shortDescription: 'Optional list of agent IDs to mark stale tasks as failed.',
		category: 'risk',
		fullDescription: 'Use during incidents to force-fail bad agent queues while allowing others to recover normally.',
		interpretations: [
			{ range: 'blank', label: 'Recommended', color: 'green', description: 'Normal behavior: recover stale tasks to pending.' },
			{ range: 'agent-id list', label: 'Targeted', color: 'yellow', description: 'Use only for known broken agents.' }
		]
	},
	strict_regime_gating_setting: {
		id: 'strict_regime_gating_setting',
		term: 'Strict Regime Gating',
		shortDescription: 'Block trades when strategy/regime compatibility checks fail.',
		category: 'risk',
		fullDescription: 'Adds a hard guardrail for incompatible market conditions.',
		interpretations: [
			{ range: 'enabled', label: 'Recommended', color: 'green', description: 'Safer default for live trading.' },
			{ range: 'disabled', label: 'Aggressive', color: 'yellow', description: 'Higher opportunity capture, higher mismatch risk.' }
		]
	},
	regime_min_confidence_setting: {
		id: 'regime_min_confidence_setting',
		term: 'Regime Minimum Confidence',
		shortDescription: 'Minimum confidence required for regime-gated execution.',
		category: 'risk',
		fullDescription: 'If detected regime confidence is below this value, strict gating can block execution.',
		interpretations: [
			{ range: '0.30 - 0.70', label: 'Recommended', color: 'green', description: 'Balances safety and execution continuity.' },
			{ range: '< 0.30', label: 'Permissive', color: 'yellow', description: 'More trades, weaker confidence guardrails.' },
			{ range: '> 0.70', label: 'Strict', color: 'yellow', description: 'Safer but may block many valid opportunities.' }
		]
	},
	allow_unknown_regime_strategies_setting: {
		id: 'allow_unknown_regime_strategies_setting',
		term: 'Allow Unknown Strategy Types',
		shortDescription: 'Permit strategies without a known regime profile through the gate.',
		category: 'risk',
		fullDescription: 'Useful while onboarding new strategies, but lowers gating strictness.',
		interpretations: [
			{ range: 'disabled', label: 'Recommended', color: 'green', description: 'Keeps gate strict and explicit.' },
			{ range: 'enabled', label: 'Flexible', color: 'yellow', description: 'Faster onboarding, weaker regime safety.' }
		]
	},
	worker_concurrency: {
		id: 'worker_concurrency',
		term: 'Worker Concurrency',
		shortDescription: 'Number of parallel workers processing 24/7 jobs.',
		category: 'optimization',
		fullDescription: 'Higher values increase throughput but consume more CPU/memory and can increase data contention.'
	},
	generation_batch_size: {
		id: 'generation_batch_size',
		term: 'Generation Batch Size',
		shortDescription: 'How many strategy candidates are generated per cycle.',
		category: 'optimization',
		fullDescription: 'Larger batches improve discovery speed but can flood downstream ranking if filters are too loose.'
	},
	ranking_top_n: {
		id: 'ranking_top_n',
		term: 'Ranking Top N',
		shortDescription: 'Number of candidates retained after ranking.',
		category: 'optimization',
		fullDescription: 'Controls survivor pressure. Smaller N is stricter; larger N preserves diversity.'
	},
	ranking_metric: {
		id: 'ranking_metric',
		term: 'Ranking Metric',
		shortDescription: 'Primary score used to sort strategies.',
		category: 'metric',
		fullDescription: 'Choose the metric that best matches your objective, then validate with secondary risk metrics before promotion.'
	},
	survivor_tier: {
		id: 'survivor_tier',
		term: 'Survivor Tier',
		shortDescription: 'Minimum quality tier allowed to continue.',
		category: 'risk',
		fullDescription: 'Set stricter thresholds to reduce noise and storage churn, at the risk of missing early-stage edges.'
	},
	nuke_noise: {
		id: 'nuke_noise',
		term: 'Nuke Noise',
		shortDescription: 'Permanently remove failed/weak strategy litter.',
		category: 'risk',
		fullDescription: 'Hard-deletes low-quality outputs instead of retaining them. Improves signal-to-noise and keeps inventories clean.'
	},
	dry_run_mode: {
		id: 'dry_run_mode',
		term: 'Dry Run Mode',
		shortDescription: 'Simulate cleanup actions without deleting data.',
		category: 'risk',
		fullDescription: 'Useful for verifying what would be removed before turning destructive cleanup on.'
	},
	rules_per_side: {
		id: 'rules_per_side',
		term: 'Rules Per Side',
		shortDescription: 'Number of entry/exit rules combined per strategy side.',
		category: 'optimization',
		fullDescription: 'Higher rule counts expand search space rapidly and raise overfitting risk.'
	},
	conditions_setting: {
		id: 'conditions_setting',
		term: 'Conditions',
		shortDescription: 'Allowed condition operators in combinator generation.',
		category: 'strategy',
		fullDescription: 'Selecting more condition types increases diversity but can produce noisier combinations.'
	},
	max_combinations: {
		id: 'max_combinations',
		term: 'Max Combinations',
		shortDescription: 'Hard cap on generated strategy permutations per run.',
		category: 'optimization',
		fullDescription: 'Prevents runaway combinatorial explosion and keeps scan runtime predictable.'
	},
	min_trades_setting: {
		id: 'min_trades_setting',
		term: 'Minimum Trades',
		shortDescription: 'Reject candidates with insufficient sample size.',
		category: 'risk',
		fullDescription: 'Low trade counts make metrics unstable and easily overfit.'
	},
	min_sharpe_setting: {
		id: 'min_sharpe_setting',
		term: 'Minimum Sharpe',
		shortDescription: 'Minimum risk-adjusted return required to pass.',
		category: 'risk',
		fullDescription: 'Strategies below this threshold are treated as low edge relative to volatility.'
	},
	min_profit_factor_setting: {
		id: 'min_profit_factor_setting',
		term: 'Minimum Profit Factor',
		shortDescription: 'Minimum gross-profit to gross-loss ratio required.',
		category: 'risk',
		fullDescription: 'Filters out candidates with weak payoff asymmetry after trading costs.'
	}
};

// Get help content by ID
export function getHelpContent(id: string): HelpContent | undefined {
	return helpContent[id];
}

// Get all content for a category
export function getHelpByCategory(category: HelpContent['category']): HelpContent[] {
	return Object.values(helpContent).filter(h => h.category === category);
}

// Search help content
export function searchHelp(query: string): HelpContent[] {
	const q = query.toLowerCase();
	return Object.values(helpContent).filter(h =>
		h.term.toLowerCase().includes(q) ||
		h.shortDescription.toLowerCase().includes(q) ||
		h.fullDescription.toLowerCase().includes(q)
	);
}
