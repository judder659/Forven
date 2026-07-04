import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	pageContext,
	inferPageKind,
	setRoute,
	setPageContext,
	clearPageEntity,
} from '../lib/stores/pageContext';

describe('pageContext', () => {
	it('infers page kinds from the path', () => {
		expect(inferPageKind('/lab/strategy/S00007')).toBe('strategy_detail');
		expect(inferPageKind('/lab')).toBe('lab');
		expect(inferPageKind('/')).toBe('dashboard');
		expect(inferPageKind('/paper-trades')).toBe('paper_trading');
		expect(inferPageKind('/live-trades')).toBe('live_trading');
		expect(inferPageKind('/data')).toBe('data_engine');
		expect(inferPageKind('/settings/agents')).toBe('settings');
	});

	it('setRoute parses the strategy entity from the URL', () => {
		setRoute('/lab/strategy/S00042');
		const ctx = get(pageContext);
		expect(ctx.page_kind).toBe('strategy_detail');
		expect(ctx.entity).toEqual({ type: 'strategy', id: 'S00042' });
	});

	it('setRoute resets stale entity/summary on navigation', () => {
		setRoute('/lab/strategy/S1');
		setPageContext({ summary: 'looking at equity curve' });
		expect(get(pageContext).summary).toBe('looking at equity curve');
		setRoute('/data');
		const ctx = get(pageContext);
		expect(ctx.page_kind).toBe('data_engine');
		expect(ctx.entity).toBeUndefined();
		expect(ctx.summary).toBeUndefined();
	});

	it('setPageContext merges and clearPageEntity drops entity/summary', () => {
		setRoute('/lab');
		setPageContext({ entity: { type: 'strategy', id: 'S9', label: 'X' }, summary: 's' });
		expect(get(pageContext).entity?.id).toBe('S9');
		clearPageEntity();
		const ctx = get(pageContext);
		expect(ctx.entity).toBeUndefined();
		expect(ctx.summary).toBeUndefined();
		expect(ctx.page_kind).toBe('lab');
	});
});
