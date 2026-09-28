import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';
import { fireEvent } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	getCatalog: vi.fn(),
	getCollectorStatus: vi.fn(),
	getDataLog: vi.fn(),
	getStorage: vi.fn(),
	getUniversePlanDiff: vi.fn(),
	getVenues: vi.fn(),
	refreshSeries: vi.fn(),
	seedUniverse: vi.fn(),
	getSlaCensus: vi.fn(),
	getJobsSummary: vi.fn(),
	listJobs: vi.fn(),
	cancelJob: vi.fn(),
	retryJob: vi.fn(),
}));
vi.mock('$lib/api/dataManager', () => api);
const toast = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/processTracker', () => ({ addToast: toast }));

import { ApiError } from '../lib/api/core';
import * as F from '../lib/api/dataManagerFixtures';
import type { CatalogRow, SlaState } from '../lib/api/dataManagerTypes';
import HealthPage from '../routes/data-next/+page.svelte';
import CatalogTable from '../lib/components/data-manager/CatalogTable.svelte';
import CoverageGrid from '../lib/components/data-manager/CoverageGrid.svelte';
import JobsDrawer from '../lib/components/data-manager/JobsDrawer.svelte';
import SectionState from '../lib/components/data-manager/SectionState.svelte';
import StateChip from '../lib/components/data-manager/StateChip.svelte';
import TypedConfirm from '../lib/components/data-manager/TypedConfirm.svelte';
import { DEFAULT_COLUMNS } from '../lib/components/data-manager/catalogViews';
import { buildCoverage } from '../lib/components/data-manager/coverage';
import { resetDataManagerState } from '../lib/stores/dataManager';

let target: HTMLDivElement;
let app: ReturnType<typeof mount> | null = null;

async function settle(rounds = 12) {
	for (let i = 0; i < rounds; i++) {
		await Promise.resolve();
		await tick();
	}
}
const text = () => target.textContent?.replace(/\s+/g, ' ') ?? '';
const button = (label: string) => [...target.querySelectorAll('button')].find((b) => b.textContent?.trim() === label);

beforeEach(() => {
	target = document.createElement('div');
	document.body.appendChild(target);
	resetDataManagerState();
	for (const fn of Object.values(api)) fn.mockReset();
	toast.mockReset();
});
afterEach(async () => {
	if (app) await unmount(app);
	app = null;
	target.remove();
});

function serveFixtures() {
	api.getSlaCensus.mockResolvedValue(F.fixtureCensus());
	api.getCatalog.mockImplementation(async (query: Record<string, unknown>) => F.fixtureCatalog(query as never));
	api.getCollectorStatus.mockResolvedValue(F.fixtureCollector());
	api.getVenues.mockResolvedValue(F.fixtureVenues());
	api.getStorage.mockResolvedValue(F.fixtureStorage());
	api.getUniversePlanDiff.mockResolvedValue(F.fixturePlanDiff());
	api.getDataLog.mockImplementation(async (query: Record<string, unknown>) => F.fixtureLog(query as never));
}

describe('Health view', () => {
	it('answers "is my data OK?" from the server’s assessments', async () => {
		serveFixtures();
		app = mount(HealthPage, { target });
		await settle(20);
		const verdict = target.querySelector('[data-testid="dm-verdict"]')!.textContent!.replace(/\s+/g, ' ');
		expect(verdict).toContain('Paper strategy S04928 is trading on data 52 min stale');
		expect(verdict).toContain('1,650 series');
		expect(target.querySelector('[data-testid="tier-live"]')!.textContent).toContain('13 fresh');
		expect(target.querySelector('[data-testid="tier-paper"]')!.textContent).toMatch(/4 fresh\s*1 late/);
		const paper = target.querySelector('[data-testid="attention-paper"]')!.textContent!.replace(/\s+/g, ' ');
		expect(paper).toContain('SOL/USDT');
		expect(paper).toContain('allowed 45 min, 52 min behind; feeds S04928 (paper)');
		expect(text()).toContain('136 of 180 planned series stored');
		expect(text()).toContain('OKX liquidations');
		// Live and paper problems come from the catalog, not the cross-tier worst list.
		expect(api.getCatalog).toHaveBeenCalledWith(expect.objectContaining({ tier: ['live', 'paper', 'pipeline'], state: ['late', 'breach', 'missing'] }), expect.anything());
	});

	it('fixes every late live and paper series with one action', async () => {
		serveFixtures();
		api.refreshSeries.mockResolvedValue({ id: 'dj-1' });
		app = mount(HealthPage, { target });
		await settle(20);
		await fireEvent.click(button('Fix all late live & paper')!);
		await settle();
		expect(api.refreshSeries).toHaveBeenCalledWith({ scope: 'late_live_paper', mode: 'refresh' });
		expect(toast).toHaveBeenCalledWith(expect.stringMatching(/late live and paper/), 'success', undefined);
	});

	it('refreshes one row and marks it queued', async () => {
		serveFixtures();
		api.refreshSeries.mockResolvedValue({ id: 'dj-2' });
		app = mount(HealthPage, { target });
		await settle(20);
		const paper = target.querySelector('[data-testid="attention-paper"]')!;
		const refresh = [...paper.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Refresh now')!;
		await fireEvent.click(refresh);
		await settle();
		expect(api.refreshSeries).toHaveBeenCalledWith({ series: [{ symbol: 'SOL-USDT', timeframe: '15m', stream: 'ohlcv', venue: 'canonical' }], mode: 'refresh' });
		expect(paper.textContent).toContain('Queued');
	});

	it('does not white-screen when the Data Manager backend is missing', async () => {
		const missing = () => Promise.reject(new ApiError(404, 'Not Found'));
		for (const fn of Object.values(api)) fn.mockImplementation(missing);
		app = mount(HealthPage, { target });
		await settle(20);
		expect(text()).toContain('Data freshness can’t be checked on this backend yet');
		expect(target.querySelectorAll('[role="status"]').length).toBeGreaterThanOrEqual(4);
		expect(text()).toContain('GET /api/data/venues');
		expect(text()).toContain('GET /api/data/collector');
	});

	it('shows a teaching setup card for an empty lake', async () => {
		serveFixtures();
		api.getSlaCensus.mockResolvedValue({ ...F.fixtureCensus(), total: 0 });
		app = mount(HealthPage, { target });
		await settle(20);
		expect(text()).toContain('Your data lake is empty');
		expect(target.querySelector('a[href="/data-next/setup"]')).not.toBeNull();
	});
});

describe('Catalog grid', () => {
	const rows = F.fixtureCatalogRows();

	function open(selected = new Set<string>()) {
		const events = { open: vi.fn(), toggle: vi.fn(), selectAll: vi.fn(), clearSelection: vi.fn(), sort: vi.fn() };
		app = mount(CatalogTable, {
			target,
			props: { rows, total: rows.length, columns: [...DEFAULT_COLUMNS], selected, sort: 'priority', order: 'desc' },
			events: {
				open: (e: CustomEvent<CatalogRow>) => events.open(e.detail),
				toggle: (e: CustomEvent) => events.toggle(e.detail),
				selectAll: () => events.selectAll(),
				clearSelection: () => events.clearSelection(),
				sort: (e: CustomEvent) => events.sort(e.detail),
			},
		});
		return events;
	}

	it('renders only a window of the 1,650 rows', async () => {
		open();
		await settle();
		const grid = target.querySelector('[data-testid="catalog-grid"]')!;
		expect(grid.getAttribute('aria-rowcount')).toBe(String(rows.length + 1));
		const rendered = target.querySelectorAll('[data-testid="catalog-row"]').length;
		expect(rendered).toBeGreaterThan(10);
		expect(rendered).toBeLessThan(60);
	});

	it('moves with the arrows, opens with Enter and selects with Space', async () => {
		const events = open();
		await settle();
		const grid = target.querySelector('[data-testid="catalog-grid"]') as HTMLElement;
		await fireEvent.keyDown(grid, { key: 'ArrowDown' });
		await fireEvent.keyDown(grid, { key: 'ArrowDown' });
		await settle();
		const active = grid.getAttribute('aria-activedescendant');
		expect(active).toMatch(/^dm-cat-row-\d$/);
		const index = Number(active!.split('-').pop());
		await fireEvent.keyDown(grid, { key: 'Enter' });
		expect(events.open).toHaveBeenCalledWith(rows[index]);
		await fireEvent.keyDown(grid, { key: ' ' });
		expect(events.toggle).toHaveBeenCalledWith({ row: rows[index], index, shift: false });
		await fireEvent.keyDown(grid, { key: 'a', ctrlKey: true });
		expect(events.selectAll).toHaveBeenCalled();
	});

	it('sorts from the headers and pairs every state colour with its word', async () => {
		const events = open();
		await settle();
		const quality = [...target.querySelectorAll('[role="columnheader"] button')].find((b) => b.textContent?.startsWith('Qual'))!;
		await fireEvent.click(quality);
		expect(events.sort).toHaveBeenCalledWith('quality');
		const firstRow = target.querySelector('[data-testid="catalog-row"]')!;
		expect(firstRow.textContent).toMatch(/Fresh|Late|Breach|Frozen|Missing/);
	});
});

describe('Coverage grid', () => {
	it('opens a stored cell with Enter and extends a selection with Shift+arrows', async () => {
		const model = buildCoverage(F.fixtureCatalogRows(), F.fixturePlanDiff(), { stream: 'ohlcv' });
		const opened = vi.fn();
		const selected = vi.fn();
		app = mount(CoverageGrid, {
			target,
			props: { model, selected: new Set<string>() },
			events: { open: (e: CustomEvent) => opened(e.detail), select: (e: CustomEvent<string[]>) => selected(e.detail) },
		});
		await settle();
		const grid = target.querySelector('[data-testid="coverage-grid"]') as HTMLElement;
		const first = model.flat[0];
		const col = first.findIndex((cell) => cell.row && cell.row.rows > 0);
		for (let i = 0; i < col; i++) await fireEvent.keyDown(grid, { key: 'ArrowRight' });
		await fireEvent.keyDown(grid, { key: 'Enter' });
		expect(opened).toHaveBeenCalledWith(expect.objectContaining({ symbol: first[col].symbol, timeframe: first[col].timeframe }));
		await fireEvent.keyDown(grid, { key: 'ArrowDown', shiftKey: true });
		expect(selected).toHaveBeenLastCalledWith([model.flat[0][col].key, model.flat[1][col].key]);
		expect(grid.querySelector('[role="gridcell"]')!.getAttribute('aria-label')).toMatch(/: (Fresh|Late|Breach|Frozen|Missing|planned|not stored)/);
	});
});

describe('Jobs drawer', () => {
	it('moves focus inside on open and closes on Escape', async () => {
		const animate = Element.prototype.animate;
		Element.prototype.animate = function () {
			return { cancel() {}, finish() {}, play() {}, pause() {}, finished: Promise.resolve(), onfinish: null, currentTime: 0 } as unknown as Animation;
		};
		try {
			const running = F.fixtureJobs().filter((j) => j.status === 'running').slice(0, 1);
			api.listJobs.mockResolvedValue({ total: 1, jobs: running });
			const closed = vi.fn();
			app = mount(JobsDrawer, { target, events: { close: () => closed() } });
			await settle();
			const dialog = document.querySelector('[role="dialog"]') as HTMLElement;
			expect(dialog.getAttribute('aria-modal')).toBe('false');
			expect(document.activeElement?.getAttribute('aria-label')).toBe('Close jobs');
			expect(dialog.textContent).toContain(running[0].title);
			expect(dialog.querySelector('[role="progressbar"]')).not.toBeNull();
			await fireEvent.keyDown(dialog, { key: 'Escape' });
			expect(closed).toHaveBeenCalled();
		} finally {
			Element.prototype.animate = animate;
		}
	});
});

describe('Shared pieces', () => {
	it('TypedConfirm only confirms the exact phrase', async () => {
		const confirmed = vi.fn();
		app = mount(TypedConfirm, { target, props: { phrase: 'delete 2 series', action: 'Move to trash' }, events: { confirm: () => confirmed() } });
		await settle();
		const input = target.querySelector('input')!;
		const submit = button('Move to trash')!;
		expect(submit.disabled).toBe(true);
		await fireEvent.input(input, { target: { value: 'delete 3 series' } });
		expect(submit.disabled).toBe(true);
		await fireEvent.input(input, { target: { value: 'delete 2 series' } });
		expect(submit.disabled).toBe(false);
		await fireEvent.click(submit);
		expect(confirmed).toHaveBeenCalledTimes(1);
	});

	it('SectionState explains a route that is not deployed and offers a retry', async () => {
		const retry = vi.fn();
		app = mount(SectionState, {
			target,
			props: { state: { status: 'unavailable', data: null, error: '', at: 1 }, what: 'Source health', endpoint: 'GET /api/data/venues' },
			events: { retry: () => retry() },
		});
		await settle();
		expect(text()).toContain('Not available yet');
		expect(text()).toContain('Source health needs GET /api/data/venues');
		await fireEvent.click(button('Check again')!);
		expect(retry).toHaveBeenCalled();
	});

	it('StateChip shows the word next to the colour, and the lag against the allowance', async () => {
		const state: SlaState = 'late';
		app = mount(StateChip, { target, props: { state, sla: { lag_seconds: 3120, allowed_seconds: 2700 }, caption: true } });
		await settle();
		expect(text()).toContain('Late');
		expect(text()).toContain('52 min / 45 min');
	});
});
