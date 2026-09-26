import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as api from '../lib/api';
import { ApiOutcomeUnknownError, fetchApi, LONG_TIMEOUT_MS } from '../lib/api/core';

// Mock fetch globally
const mockFetch = vi.fn();
global.fetch = mockFetch;

describe('API Client', () => {
	beforeEach(() => {
		mockFetch.mockReset();
		window.localStorage.clear();
	});

	afterEach(() => {
		vi.clearAllMocks();
	});

	describe('getSymbols', () => {
		it('should fetch symbols successfully', async () => {
			const mockSymbols = ['BTC/USDT', 'ETH/USDT'];
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockSymbols)
			});

			const result = await api.getSymbols();
			const [, requestInit] = mockFetch.mock.calls[0] as [string, RequestInit];
			const headers = requestInit.headers as Headers;
			expect(headers.get('Content-Type')).toBe('application/json');
			expect(result).toEqual(mockSymbols);
		});

		it('should include api/operator keys from local storage when configured', async () => {
			window.localStorage.setItem('forven_api_key', 'api-key-123');
			window.localStorage.setItem('forven_operator_key', 'op-key-456');
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(['BTC/USDT'])
			});

			await api.getSymbols();
			const [, requestInit] = mockFetch.mock.calls[0] as [string, RequestInit];
			const headers = requestInit.headers as Headers;
			expect(headers.get('X-API-Key')).toBe('api-key-123');
			expect(headers.get('X-Operator-Key')).toBe('op-key-456');
		});

		it('should throw on error response', async () => {
			mockFetch.mockResolvedValue({
				ok: false,
				status: 500,
				text: () => Promise.resolve(JSON.stringify({ detail: 'Server error' })),
				json: () => Promise.resolve({ detail: 'Server error' })
			});

			await expect(api.getSymbols()).rejects.toThrow('Server error');
		});
	});

	describe('getDatasets', () => {
		it('should fetch datasets successfully', async () => {
			const mockDatasets = [
				{
					symbol: 'BTC/USDT',
					timeframe: '1h',
					source: 'binance',
					start_ts: '2024-01-01T00:00:00Z',
					end_ts: '2024-06-01T00:00:00Z',
					row_count: 1000
				}
			];
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockDatasets)
			});

			const result = await api.getDatasets();

			expect(result).toEqual(mockDatasets);
		});
	});

	describe('ideas barrel exports', () => {
		it('exposes the idea client helpers', () => {
			expect(typeof api.submitIdea).toBe('function');
			expect(typeof api.previewIdeaUrl).toBe('function');
			expect(typeof api.getIdea).toBe('function');
		});
	});

	describe('fetchData', () => {
		it('uses the direct fetch endpoint for bounded requests', async () => {
			const dataset = {
				symbol: 'BTC/USDT',
				timeframe: '1h',
				source: 'binance',
				start_ts: '2024-01-01T00:00:00Z',
				end_ts: '2024-01-31T00:00:00Z',
				row_count: 1000
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(dataset)
			});

			const result = await api.fetchData('BTC/USDT', '1h', 'binance', 1000);

			expect(result).toEqual(dataset);
			expect(mockFetch).toHaveBeenCalledWith(
				'/api/fetch?symbol=BTC%2FUSDT&timeframe=1h&exchange=binance&limit=1000',
				expect.objectContaining({ method: 'POST' })
			);
		});

		it('polls the ingestion endpoint for all-available requests', async () => {
			const dataset = {
				symbol: 'ETH/USDT',
				timeframe: '1m',
				source: 'binance',
				start_ts: '2024-01-01T00:00:00Z',
				end_ts: '2024-01-31T00:00:00Z',
				row_count: 44_640
			};
			const progress = vi.fn();
			mockFetch
				.mockResolvedValueOnce({
					ok: true,
					json: () => Promise.resolve({
						id: 'run-123',
						symbol: 'ETH/USDT',
						timeframe: '1m',
						source: 'binance',
						status: 'pending',
						bars_fetched: 0,
						bars_new: 0,
						bars_updated: 0,
						error: null,
						idempotency_key: null,
						prior_version_id: null,
						new_version_id: null,
						started_at: '2026-03-14T00:00:00Z',
						completed_at: null,
						duration_ms: null
					})
				})
				.mockResolvedValueOnce({
					ok: true,
					json: () => Promise.resolve({
						id: 'run-123',
						symbol: 'ETH/USDT',
						timeframe: '1m',
						source: 'binance',
						status: 'completed',
						bars_fetched: 44_640,
						bars_new: 44_640,
						bars_updated: 0,
						error: null,
						idempotency_key: null,
						prior_version_id: null,
						new_version_id: null,
						started_at: '2026-03-14T00:00:00Z',
						completed_at: '2026-03-14T00:05:00Z',
						duration_ms: 300_000
					})
				})
				.mockResolvedValueOnce({
					ok: true,
					json: () => Promise.resolve(dataset)
				});

			const result = await api.fetchData(
				'ETH/USDT',
				'1m',
				'binance',
				1000,
				undefined,
				undefined,
				true,
				undefined,
				progress
			);

			expect(result).toEqual(dataset);
			expect(mockFetch).toHaveBeenNthCalledWith(
				1,
				'/api/data/ingestion/submit?symbol=ETH%2FUSDT&timeframe=1m&exchange=binance&all_available=true',
				expect.objectContaining({ method: 'POST' })
			);
			expect(mockFetch).toHaveBeenNthCalledWith(
				2,
				'/api/data/ingestion/runs/run-123',
				expect.anything()
			);
			expect(mockFetch).toHaveBeenNthCalledWith(
				3,
				'/api/datasets/ETH%2FUSDT/1m',
				expect.anything()
			);
			expect(progress).toHaveBeenCalledWith(
				expect.objectContaining({ message: 'Queueing ETH/USDT 1m...' })
			);
			expect(progress).toHaveBeenCalledWith(
				expect.objectContaining({ message: 'Finalizing ETH/USDT 1m...' })
			);
		});

		it('surfaces ingestion failures without waiting for the direct fetch timeout', async () => {
			mockFetch
				.mockResolvedValueOnce({
					ok: true,
					json: () => Promise.resolve({
						id: 'run-456',
						symbol: 'ETH/USDT',
						timeframe: '1m',
						source: 'binance',
						status: 'pending',
						bars_fetched: 0,
						bars_new: 0,
						bars_updated: 0,
						error: null,
						idempotency_key: null,
						prior_version_id: null,
						new_version_id: null,
						started_at: '2026-03-14T00:00:00Z',
						completed_at: null,
						duration_ms: null
					})
				})
				.mockResolvedValueOnce({
					ok: true,
					json: () => Promise.resolve({
						id: 'run-456',
						symbol: 'ETH/USDT',
						timeframe: '1m',
						source: 'binance',
						status: 'failed',
						bars_fetched: 18_000,
						bars_new: 0,
						bars_updated: 0,
						error: 'signal timed out',
						idempotency_key: null,
						prior_version_id: null,
						new_version_id: null,
						started_at: '2026-03-14T00:00:00Z',
						completed_at: '2026-03-14T00:08:00Z',
						duration_ms: 480_000
					})
				});

			await expect(
				api.fetchData('ETH/USDT', '1m', 'binance', 1000, undefined, undefined, true)
			).rejects.toThrow('signal timed out');
		});
	});

	describe('getStrategies', () => {
		it('should fetch strategies successfully', async () => {
			const mockStrategies = {
				strategies: [
					{
						name: 'rsi_strategy',
						version: '1.0.0',
						description: 'RSI-based strategy',
						parameters: {
							period: { type: 'int', default: 14, min: 5, max: 50 }
						}
					}
				]
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockStrategies)
			});

			const result = await api.getStrategies();

			expect(result.strategies).toHaveLength(1);
			expect(result.strategies[0].name).toBe('rsi_strategy');
		});
	});

	describe('getStrategy', () => {
		it('should fetch a specific strategy', async () => {
			const mockStrategy = {
				name: 'rsi_strategy',
				version: '1.0.0',
				description: 'RSI strategy',
				parameters: {}
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockStrategy)
			});

			const result = await api.getStrategy('rsi_strategy');

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/strategies/rsi_strategy',
				expect.anything()
			);
			expect(result.name).toBe('rsi_strategy');
		});

		it('should handle strategy not found', async () => {
			mockFetch.mockResolvedValue({
				ok: false,
				status: 404,
				text: () => Promise.resolve(JSON.stringify({ detail: 'Strategy not found' })),
				json: () => Promise.resolve({ detail: 'Strategy not found' })
			});

			await expect(api.getStrategy('nonexistent')).rejects.toThrow('Strategy not found');
		});
	});

	describe('submitBacktest', () => {
		it('submits long interactive runs as background jobs', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: async () => ({ job_id: 'bt-long', status: 'queued' }),
			});
			const accepted = await api.submitBacktest({
				strategy_id: 'S09735', strategy_name: 'SOL breakout', symbol: 'SOL', timeframe: '1h',
				start: '2021-09-08', end: '2026-09-08', preserve_result: true,
			}, { background: true });
			expect(mockFetch).toHaveBeenCalledTimes(1);
			expect(mockFetch.mock.calls[0][0]).toBe('/api/backtests?background=true');
			expect(JSON.parse(mockFetch.mock.calls[0][1].body)).toMatchObject({
				strategy_id: 'S09735', start: '2021-09-08', end: '2026-09-08', preserve_result: true,
			});
			expect(accepted.status).toBe('queued');
		});

		it('reports an uncertain submission on timeout without replaying it', async () => {
			mockFetch.mockRejectedValueOnce(new DOMException('signal timed out', 'TimeoutError'));
			await expect(api.submitBacktest({
				strategy_id: 'S09735', strategy_name: 'SOL breakout', symbol: 'SOL', timeframe: '1h',
			}, { background: true })).rejects.toBeInstanceOf(ApiOutcomeUnknownError);
			expect(mockFetch).toHaveBeenCalledTimes(1);
		});

		it('should submit backtest request', async () => {
			const mockResponse = { job_id: 'test-job-123', status: 'queued' };
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResponse)
			});

			const result = await api.submitBacktest({
				strategy_name: 'rsi_strategy',
				symbol: 'BTC/USDT',
				timeframe: '1h'
			});

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/backtests',
				expect.objectContaining({
					method: 'POST',
					body: expect.any(String)
				})
			);
			expect(result.job_id).toBe('test-job-123');
		});

		it('should include strategy_id in submit payload when provided', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({ job_id: 'job-sid-1', status: 'queued' })
			});

			await api.submitBacktest({
				strategy_id: 'S00042',
				strategy_name: 'BTC-MACD-S00042',
				symbol: 'BTC',
				timeframe: '1h'
			});

			const [, requestInit] = mockFetch.mock.calls[0] as [string, RequestInit];
			const body = JSON.parse(String(requestInit.body || '{}')) as Record<string, unknown>;
			expect(body.strategy_id).toBe('S00042');
		});

		it('should support container-bound submit flow using container strategy_id', async () => {
			mockFetch
				.mockResolvedValueOnce({
					ok: true,
					json: () => Promise.resolve({
						strategy: {
							id: 'S00077',
							name: 'BTC-MACD-S00077',
							state: 'backtesting',
							source: 'manual',
							source_ref: null,
							owner: 'brain',
							symbol: 'BTC',
							timeframe: '1h',
							definition_json: null,
							dataset_hash: null,
							policy_version: 1,
							build_version: null,
							metrics_json: null,
							paper_session_id: null,
							paper_started_at: null,
							last_policy_result_json: null,
							blocked_reason: null,
							model: null,
							model_id: null,
							created_at: '2026-03-03T00:00:00Z',
							updated_at: '2026-03-03T00:00:00Z',
							state_changed_at: null,
							failed_at: null,
							retention_expires_at: null
						},
						configuration: { symbol: 'BTC', timeframe: '1h', params: {} },
						history: { all: [], backtests: [], optimizations: [], walk_forward: [] },
						execution: { trades: [], positions: [] },
						events: []
					})
				})
				.mockResolvedValueOnce({
					ok: true,
					json: () => Promise.resolve({ job_id: 'job-container-1', status: 'queued' })
				});

			const container = await api.getStrategyContainer('S00077');
			await api.submitBacktest({
				strategy_id: container.strategy.id,
				strategy_name: container.strategy.name,
				symbol: 'BTC',
				timeframe: '1h'
			});

			expect(mockFetch).toHaveBeenNthCalledWith(
				1,
				'/api/strategies/S00077/container',
				expect.anything()
			);
			expect(mockFetch).toHaveBeenNthCalledWith(
				2,
				'/api/backtests',
				expect.objectContaining({ method: 'POST', body: expect.any(String) })
			);
			const [, requestInit] = mockFetch.mock.calls[1] as [string, RequestInit];
			const body = JSON.parse(String(requestInit.body || '{}')) as Record<string, unknown>;
			expect(body.strategy_id).toBe('S00077');
		});

		it('should use the long-running timeout for backtest submits', async () => {
			const timeoutSpy = vi.spyOn(AbortSignal, 'timeout');
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({ job_id: 'job-timeout-1', status: 'queued' })
			});

			await api.submitBacktest({
				strategy_name: 'rsi_strategy',
				symbol: 'BTC/USDT',
				timeframe: '1h'
			});

			expect(timeoutSpy).toHaveBeenLastCalledWith(LONG_TIMEOUT_MS);
		});
	});

	describe('getResultChartContext', () => {
		it('should fetch chart context for a stored result', async () => {
			const mockResponse = {
				result_id: 'B-CHART-001',
				source: 'artifact',
				bars: [
					{
						timestamp: '2025-01-10T00:00:00Z',
						open: 100,
						high: 105,
						low: 99,
						close: 103,
						volume: 1200
					}
				],
				entry_markers: [{ timestamp: '2025-01-10T02:00:00Z', price: 102 }],
				exit_markers: [{ timestamp: '2025-01-10T06:00:00Z', price: 104 }],
				main_indicators: [],
				sub_indicators: [
					{
						name: 'MACD',
						color: '#22d3ee',
						data: [{ timestamp: '2025-01-10T00:00:00Z', value: 1.2 }]
					}
				],
				strategy_name: 'BTC-MACD-S00001',
				strategy_meta: 'BTC | 1h | 2025-01-10 -> 2025-01-20',
				strategy_params: { fast: 12, slow: 26, signal: 9 },
				warnings: []
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResponse)
			});

			const result = await api.getResultChartContext('B-CHART-001');

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/results/B-CHART-001/chart-context',
				expect.anything()
			);
			expect(result.source).toBe('artifact');
			expect(result.entry_markers).toHaveLength(1);
			expect(result.strategy_params.fast).toBe(12);
		});
	});

	describe('getStrategyContainer', () => {
		it('should fetch unified strategy container payload', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({
					strategy: {
						id: 'S00001',
						name: 'BTC-MACD-S00001',
						state: 'backtesting',
						source: 'manual',
						source_ref: null,
						owner: 'brain',
						symbol: 'BTC',
						timeframe: '1h',
						definition_json: null,
						dataset_hash: null,
						policy_version: 1,
						build_version: null,
						metrics_json: null,
						paper_session_id: null,
						paper_started_at: null,
						last_policy_result_json: null,
						blocked_reason: null,
						model: null,
						model_id: null,
						created_at: '2026-03-03T00:00:00Z',
						updated_at: '2026-03-03T00:00:00Z',
						state_changed_at: null,
						failed_at: null,
						retention_expires_at: null
					},
					configuration: { symbol: 'BTC', timeframe: '1h', params: { fast: 12, slow: 26 } },
					history: {
						all: [{ result_id: 'R-1', strategy_id: 'S00001', result_type: 'backtest', symbol: 'BTC', timeframe: '1h', created_at: '2026-03-03T01:00:00Z', metrics: {}, config: {}, start_date: null, end_date: null, deleted_at: null }],
						backtests: [{ result_id: 'R-1', strategy_id: 'S00001', result_type: 'backtest', symbol: 'BTC', timeframe: '1h', created_at: '2026-03-03T01:00:00Z', metrics: {}, config: {}, start_date: null, end_date: null, deleted_at: null }],
						optimizations: [],
						walk_forward: []
					},
					execution: { trades: [], positions: [] },
					events: []
				})
			});

			const payload = await api.getStrategyContainer('S00001');
			expect(mockFetch).toHaveBeenCalledWith('/api/strategies/S00001/container', expect.anything());
			expect(payload.strategy.id).toBe('S00001');
			expect(payload.history.backtests).toHaveLength(1);
		});
	});

	describe('getJob', () => {
		it('should fetch job status', async () => {
			const mockJob = {
				id: 'test-job-123',
				type: 'backtest',
				status: 'running',
				created_at: '2024-01-01T00:00:00Z',
				updated_at: '2024-01-01T00:00:00Z'
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockJob)
			});

			const result = await api.getJob('test-job-123');

			expect(result.id).toBe('test-job-123');
			expect(result.status).toBe('running');
		});
	});

	describe('getResults', () => {
		it('should fetch results list', async () => {
			const mockResults = [
				{
					id: 'result-1',
					job_id: 'job-1',
					strategy_name: 'rsi_strategy',
					symbol: 'BTC/USDT',
					timeframe: '1h',
					created_at: '2024-01-01T00:00:00Z',
					total_return: 10.5,
					sharpe_ratio: 1.2
				}
			];
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResults)
			});

			const result = await api.getResults();

			expect(result).toHaveLength(1);
			expect(result[0].total_return).toBe(10.5);
		});
	});

	describe('paper chart APIs', () => {
		it('should include timeframe when fetching session indicators', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({
					session_id: 'compat:strategy:S00338',
					config: {},
					indicators: {}
				})
			});

			await api.getSessionIndicators('compat:strategy:S00338', ['ema_fast', 'atr_14'], 120, '1m');

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/paper/sessions/compat:strategy:S00338/indicators?indicators=ema_fast%2Catr_14&limit=120&timeframe=1m',
				expect.anything()
			);
		});
	});

	describe('checkHealth', () => {
		it('should check backend health', async () => {
			const mockHealth = { status: 'healthy', version: '1.0.0' };
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockHealth)
			});

			const result = await api.checkHealth();

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/health',
				expect.objectContaining({
					signal: expect.anything()
				})
			);
			expect(result.status).toBe('healthy');
		});
	});

	describe('Forven API Contracts', () => {
		it('should submit brain chat as async task', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				status: 202,
				json: () => Promise.resolve({ ok: true, task_id: 186 })
			});

			const result = await api.postBrainChat('hello', '/agents');

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/brain/chat',
				expect.objectContaining({
					method: 'POST',
					body: JSON.stringify({ message: 'hello', context: '/agents' })
				})
			);
			expect(result).toEqual({ ok: true, task_id: 186 });
		});

		it('should omit entity fields from brain chat body when no entity is in scope', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				status: 202,
				json: () => Promise.resolve({ ok: true, task_id: 187 })
			});

			await api.postBrainChat('hello', '/agents');

			const [, requestInit] = mockFetch.mock.calls[0] as [string, RequestInit];
			const body = JSON.parse(String(requestInit.body || '{}')) as Record<string, unknown>;
			expect(body).toEqual({ message: 'hello', context: '/agents' });
			expect(body.entity_type).toBeUndefined();
			expect(body.entity_id).toBeUndefined();
		});

		it('should thread entity_type/entity_id into brain command chat when provided', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				status: 202,
				json: () => Promise.resolve({ ok: true, task_id: 188 })
			});

			await api.postBrainChat('tell me about this one', '/lab/strategy/S00719', undefined, {
				entity_type: 'strategy',
				entity_id: 'S00719'
			});

			const [url, requestInit] = mockFetch.mock.calls[0] as [string, RequestInit];
			expect(url).toBe('/api/brain/chat');
			const body = JSON.parse(String(requestInit.body || '{}')) as Record<string, unknown>;
			expect(body.entity_type).toBe('strategy');
			expect(body.entity_id).toBe('S00719');
		});

		it('should thread entity_type/entity_id into direct brain chat when provided', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({ ok: true, response: 'It is a momentum strategy.', mode: 'direct' })
			});

			await api.postBrainChatDirect('what is this?', '/lab/strategy/S00719', undefined, {
				entity_type: 'strategy',
				entity_id: 'S00719'
			});

			const [url, requestInit] = mockFetch.mock.calls[0] as [string, RequestInit];
			expect(url).toBe('/api/brain/chat/direct');
			const body = JSON.parse(String(requestInit.body || '{}')) as Record<string, unknown>;
			expect(body.entity_type).toBe('strategy');
			expect(body.entity_id).toBe('S00719');
		});

		it('should not send entity fields when only one half is provided', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({ ok: true, response: 'ok', mode: 'direct' })
			});

			await api.postBrainChatDirect('hi', undefined, undefined, { entity_type: 'strategy' });

			const [, requestInit] = mockFetch.mock.calls[0] as [string, RequestInit];
			const body = JSON.parse(String(requestInit.body || '{}')) as Record<string, unknown>;
			expect(body.entity_type).toBeUndefined();
			expect(body.entity_id).toBeUndefined();
		});

		it('should surface error_code on a failed direct brain chat', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({
					ok: false,
					error: 'no api credentials configured',
					error_code: 'provider_unconfigured',
					retryable: false,
					mode: 'direct'
				})
			});

			const result = await api.postBrainChatDirect('hello');
			expect(result.ok).toBe(false);
			expect(result.error_code).toBe('provider_unconfigured');
			expect(result.retryable).toBe(false);
		});

		it('should read pending brain task with accepted status payload', async () => {
			const pendingPayload = {
				ok: true,
				status: 'pending',
				result: null,
				error: null,
				created_at: '2026-02-21 21:18:56',
				completed_at: null
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				status: 202,
				json: () => Promise.resolve(pendingPayload)
			});

			const result = await api.getBrainChatResult(186);

			expect(mockFetch).toHaveBeenCalledWith('/api/brain/chat/186', expect.anything());
			expect(result.status).toBe('pending');
			expect(result.result).toBeNull();
		});

		it('should surface 404 for missing agent terminal', async () => {
			mockFetch.mockResolvedValue({
				ok: false,
				status: 404,
				text: () => Promise.resolve(JSON.stringify({ detail: 'Agent not found' })),
				json: () => Promise.resolve({ detail: 'Agent not found' })
			});

			await expect(api.getForvenAgentTerminal('missing-agent')).rejects.toMatchObject({
				name: 'ApiError',
				status: 404,
				message: 'Agent not found'
			});
		});
	});

	describe('Drop Zone', () => {
		it('should get drop zone status', async () => {
			const mockStatus = {
				directory: '/path/to/dropzone',
				file_count: 3,
				loaded_strategies: ['strategy1', 'strategy2']
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockStatus)
			});

			const result = await api.getDropZoneStatus();

			expect(result.file_count).toBe(3);
			expect(result.loaded_strategies).toHaveLength(2);
		});

		it('should reload drop zone', async () => {
			const mockResponse = {
				loaded: ['strategy1'],
				errors: {}
			};
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockResponse)
			});

			const result = await api.reloadDropZone();

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/dropzone/reload',
				expect.objectContaining({ method: 'POST' })
			);
			expect(result.loaded).toContain('strategy1');
		});

		it('should register a single strategy file for AI Drop Zone intake', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve({
					module_name: 'btc_ai_dropzone_wave_test',
					type_name: 'ai_dropzone_wave_test',
					strategy_id: 'S00999',
					asset: 'BTC',
					certified: true,
					certification_error: null,
					file_name: 'btc_ai_dropzone_wave_test.py',
					source: 'ai_dropzone',
					source_ref: 'C:\\\\temp\\\\btc_ai_dropzone_wave_test.py',
					stage: 'quick_screen'
				})
			});

			const result = await api.registerStrategyFile({
				file_path: 'C:\\temp\\btc_ai_dropzone_wave_test.py'
			});

			expect(mockFetch).toHaveBeenCalledWith(
				'/api/strategies/intake/register-file',
				expect.objectContaining({
					method: 'POST',
					body: JSON.stringify({ file_path: 'C:\\temp\\btc_ai_dropzone_wave_test.py' })
				})
			);
			expect(result.source).toBe('ai_dropzone');
			expect(result.stage).toBe('quick_screen');
		});
	});

	describe('Data Sources', () => {
		it('should get available data sources', async () => {
			const mockSources = [
				{
					id: 'ccxt',
					name: 'CCXT',
					description: 'Crypto exchanges',
					asset_types: ['crypto'],
					available: true,
					requires_key: false
				}
			];
			mockFetch.mockResolvedValueOnce({
				ok: true,
				json: () => Promise.resolve(mockSources)
			});

			const result = await api.getDataSources();

			expect(result[0].id).toBe('ccxt');
		});
	});

	describe('Error Handling', () => {
		it('does not retry a mutation after a network-level failure', async () => {
			mockFetch.mockRejectedValue(new TypeError('Failed to fetch'));

			await expect(fetchApi('/tasks', {
				method: 'POST',
				body: JSON.stringify({ name: 'one-shot' }),
			})).rejects.toBeInstanceOf(ApiOutcomeUnknownError);
			expect(mockFetch).toHaveBeenCalledTimes(1);
		});

		it('should handle network errors', async () => {
			mockFetch.mockRejectedValue(new Error('Network error'));

			await expect(api.getSymbols()).rejects.toThrow('Network error');
		});

		it('should handle malformed JSON response', async () => {
			mockFetch.mockResolvedValueOnce({
				ok: false,
				status: 500,
				text: () => Promise.resolve('Invalid JSON'),
				json: () => Promise.reject(new Error('Invalid JSON'))
			});

			await expect(api.getSymbols()).rejects.toThrow();
		});
	});
});
