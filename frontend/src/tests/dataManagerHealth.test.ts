import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { FIXTURE_NOW, fixtureCatalogRows, fixtureCensus } from '../lib/api/dataManagerFixtures';
import type { SlaCensus, SlaSeriesRow, SlaState, SlaTier } from '../lib/api/dataManagerTypes';
import {
	buildDataNavIndicator,
	consumersText,
	groupAttention,
	healthVerdict,
	seriesName,
	whyText,
} from '../lib/components/data-manager/health';
import { navBadges, setNavIndicators } from '../lib/stores/navMetrics';

function withCounts(census: SlaCensus, tier: SlaTier, counts: Partial<Record<SlaState, number>>): SlaCensus {
	return { ...census, by_tier: { ...census.by_tier, [tier]: { fresh: 0, late: 0, breach: 0, frozen: 0, missing: 0, ...counts } } };
}

const row = (overrides: Partial<SlaSeriesRow> & { tier: SlaTier; state: SlaState }): SlaSeriesRow => ({
	symbol: 'BTC-USDT',
	display_symbol: 'BTC/USDT',
	timeframe: '4h',
	stream: 'ohlcv',
	venue: 'canonical',
	sla: { tier: overrides.tier, state: overrides.state, lag_seconds: 3 * 3600 + 5 * 60, allowed_seconds: 8 * 3600 / 4, ratio: 1.5, last_bar_ts: FIXTURE_NOW, priority: 150 },
	consumers: { tier: overrides.tier, count: 1, top: [{ kind: 'strategy', id: 'S01566', name: 'BTC trend follower', stage: 'live_graduated' }] },
	frozen: false,
	frozen_reason: null,
	...overrides,
});

describe('Health verdict', () => {
	const census = fixtureCensus();

	it('the fixture lake: live current, one paper series late', () => {
		const verdict = healthVerdict(census, census.worst);
		expect(census.by_tier.live.fresh).toBeGreaterThan(0);
		expect(census.by_tier.paper.late).toBe(1);
		expect(verdict.tone).toBe('warn');
		expect(verdict.headline).toBe('1 series that paper strategies trade on is late');
	});

	it('names the strategy when exactly one live series is behind', () => {
		const c = withCounts(census, 'live', { fresh: 12, breach: 1 });
		const verdict = healthVerdict(c, [row({ tier: 'live', state: 'breach' })]);
		expect(verdict.tone).toBe('bad');
		expect(verdict.headline).toBe('Live strategy S01566 is trading on data 3 h 5 m stale');
		expect(verdict.details).toContain('Paper: 1 series late');
	});

	it('says what is missing for a live strategy with no data', () => {
		const c = withCounts(withCounts(census, 'live', { missing: 1 }), 'paper', { fresh: 3 });
		const verdict = healthVerdict(c, [row({ tier: 'live', state: 'missing', sla: { tier: 'live', state: 'missing', lag_seconds: null, allowed_seconds: 7200, ratio: null, last_bar_ts: null, priority: 1000 } })]);
		expect(verdict.headline).toBe('Live strategy S01566 has no data for BTC-USDT 4h');
		expect(verdict.tone).toBe('bad');
	});

	it('falls back to counts when the rows are not at hand', () => {
		const c = withCounts(census, 'live', { late: 2, fresh: 11 });
		const verdict = healthVerdict(c, []);
		expect(verdict.headline).toBe('2 series that live strategies trade on are late');
		expect(verdict.tone).toBe('warn');
	});

	it('is green when live and paper are current, whatever research does', () => {
		const c = withCounts(withCounts(census, 'paper', { fresh: 5 }), 'pipeline', { fresh: 3 });
		const verdict = healthVerdict(c, c.worst);
		expect(verdict.tone).toBe('ok');
		expect(verdict.headline).toBe('Live and paper data is current');
		expect(verdict.details.some((d) => d.startsWith('Research:'))).toBe(true);
	});

	it('handles an empty lake and a lake nobody trades on', () => {
		expect(healthVerdict({ ...census, total: 0 }).headline).toBe('No market data is stored yet');
		const idleOnly = withCounts(withCounts(withCounts(census, 'live', {}), 'paper', {}), 'pipeline', {});
		expect(healthVerdict(idleOnly).headline).toBe('No live or paper strategy reads market data yet');
	});
});

describe('Needs attention', () => {
	it('groups by tier, live first, most overdue first, and counts what is hidden', () => {
		const census = fixtureCensus();
		const rows: SlaSeriesRow[] = [
			...census.worst,
			row({ tier: 'paper', state: 'late', symbol: 'SOL-USDT', timeframe: '15m' }),
		];
		const groups = groupAttention(rows, census, 3);
		expect(groups.map((g) => g.tier)).toEqual(['paper', 'pipeline', 'universe', 'idle']);
		const research = groups.find((g) => g.tier === 'universe')!;
		expect(research.rows).toHaveLength(3);
		expect(research.hidden).toBe(research.total - 3);
		expect(research.rows[0].sla.priority).toBeGreaterThanOrEqual(research.rows[2].sla.priority);
	});

	it('explains each row in plain words', () => {
		const sol = fixtureCatalogRows().find((r) => r.id === 'ohlcv:canonical:SOL-USDT:15m')!;
		expect(sol.sla.state).toBe('late');
		expect(whyText(sol)).toBe('allowed 45 min, 52 min behind; feeds S04928 (paper)');
		const missing = fixtureCatalogRows().find((r) => r.sla.state === 'missing')!;
		expect(whyText(missing)).toBe('nothing stored yet; needed by S10912 and wf-2291 (pipeline)');
		expect(consumersText({ tier: 'live', count: 4, top: [{ kind: 'strategy', id: 'S1', name: '' }, { kind: 'strategy', id: 'S2', name: '' }] })).toBe('S1, S2 and 2 more (live)');
		expect(seriesName({ symbol: 'ETH-USDT', timeframe: '8h', stream: 'funding', venue: 'canonical' })).toBe('ETH-USDT funding 8h');
	});
});

describe('/data nav indicator', () => {
	const census = fixtureCensus();

	it('counts live and paper problems only', () => {
		const indicator = buildDataNavIndicator(census);
		expect(indicator.kind).toBe('count');
		expect(indicator.severity).toBe('warn');
		expect(indicator.count).toBe(1);
		expect(indicator.summary).toBe('1 paper series is late');
		// The census ranks `worst` across tiers, so the paper row may not be in it:
		// a counts stand-in still changes with the live/paper counts.
		expect(indicator.item_ids).toEqual(['counts:0.0.0.1']);
		const withRow = buildDataNavIndicator({ ...census, worst: [row({ tier: 'paper', state: 'late', symbol: 'SOL-USDT', timeframe: '15m' })] });
		expect(withRow.item_ids).toEqual(['ohlcv:canonical:SOL-USDT:15m:late']);
	});

	it('a live breach is a standing STALE pill', () => {
		const indicator = buildDataNavIndicator(withCounts(census, 'live', { breach: 2 }));
		expect(indicator).toMatchObject({ kind: 'status', severity: 'danger', label: 'STALE', count: 3 });
	});

	it('is empty when live and paper are current', () => {
		expect(buildDataNavIndicator(withCounts(census, 'paper', { fresh: 4 })).kind).toBe('none');
	});

	it('becomes the /data sidebar badge', () => {
		setNavIndicators({ '/data': buildDataNavIndicator(census) });
		const badge = get(navBadges)['/data'];
		expect(badge).toMatchObject({ kind: 'count', count: 1 });
	});
});
