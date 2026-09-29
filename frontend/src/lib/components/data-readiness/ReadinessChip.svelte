<script lang="ts">
	// Is the data this strategy needs stored, fit and current? One verdict chip,
	// expanding to the requirements behind it, each with a fix link to Get data.
	// Renders nothing when the check is unavailable: the host page never breaks
	// because readiness failed.
	import { onDestroy } from 'svelte';
	import { checkReadiness, getStrategyReadiness, readinessFixHref, type ReadinessQuery } from '$lib/api/dataReadiness';
	import type { ReadinessReport, ReadinessRequirement } from '$lib/api/dataManagerTypes';

	/** A registered strategy; checked on its own symbol and timeframe. Wins over `query`. */
	export let strategyId: string | null = null;
	/** A strategy being written. */
	export let query: ReadinessQuery | null = null;
	export let debounceMs = 600;
	/** Which edge of the chip the requirement panel lines up with. */
	export let align: 'left' | 'right' = 'left';

	const VERDICT = {
		ready: { word: 'Data ready', chip: 'border-emerald-900 text-emerald-400', dot: 'bg-emerald-400' },
		needs_data: { word: 'Needs data', chip: 'border-amber-900 text-amber-400', dot: 'bg-amber-400' },
		blocked: { word: 'Data blocked', chip: 'border-red-900 text-red-400', dot: 'bg-red-400' },
	} as const;
	const STATUS: Record<ReadinessRequirement['status'], { word: string; tone: string }> = {
		ok: { word: 'OK', tone: 'border-emerald-900 text-emerald-400' },
		warn: { word: 'Warn', tone: 'border-amber-900 text-amber-400' },
		missing: { word: 'Missing', tone: 'border-sc-line2 text-sc-ink2' },
		blocked: { word: 'Blocked', tone: 'border-red-900 text-red-400' },
	};

	let report: ReadinessReport | null = null;
	let loading = false;
	let open = false;
	let seq = 0;
	let lastKey = '';
	let timer: ReturnType<typeof setTimeout> | undefined;
	onDestroy(() => clearTimeout(timer));

	$: key = strategyId
		? `id:${strategyId}`
		: query && query.symbol?.trim() && query.timeframe
			? `q:${JSON.stringify(query)}`
			: '';
	$: schedule(key);

	function schedule(next: string) {
		if (next === lastKey) return;
		lastKey = next;
		clearTimeout(timer);
		const id = ++seq;
		if (!next) {
			report = null;
			loading = false;
			return;
		}
		loading = true;
		timer = setTimeout(() => void load(id), debounceMs);
	}

	async function load(id: number) {
		try {
			const result = strategyId
				? await getStrategyReadiness(strategyId)
				: await checkReadiness({ ...(query as ReadinessQuery), symbol: (query as ReadinessQuery).symbol.trim() });
			if (id === seq) report = result;
		} catch {
			if (id === seq) report = null;
		} finally {
			if (id === seq) loading = false;
		}
	}

	$: tone = report ? VERDICT[report.verdict] : null;
	$: counted = !report
		? ''
		: report.verdict === 'blocked'
			? countOf('blocked', 'issue')
			: report.verdict === 'needs_data'
				? countOf('missing', 'fix')
				: countOf('warn', 'warning');

	function countOf(status: ReadinessRequirement['status'], noun: string): string {
		const n = report?.requirements.filter((r) => r.status === status).length ?? 0;
		return n ? `${n} ${noun}${n === 1 ? '' : 's'}` : '';
	}
</script>

<svelte:window on:keydown={(e) => { if (open && e.key === 'Escape') open = false; }} />

{#if report && tone}
	<div class="relative inline-block text-left" data-testid="readiness">
		<button type="button" on:click={() => (open = !open)} aria-expanded={open} title={report.summary}
			data-testid="readiness-chip" data-verdict={report.verdict}
			class="rounded-md inline-flex items-center gap-1.5 border bg-sc-bg px-2 py-0.5 text-[12px] transition-opacity {tone.chip} {loading ? 'opacity-60' : ''}">
			<span class="h-1.5 w-1.5 {tone.dot}" aria-hidden="true"></span>
			<span>{tone.word}</span>
			{#if counted}<span class="normal-case tracking-normal text-sc-ink2">· {counted}</span>{/if}
		</button>
		{#if open}
			<div class="rounded-md absolute {align === 'right' ? 'right-0' : 'left-0'} top-full z-30 mt-1 w-[28rem] max-w-[90vw] border border-sc-line bg-sc-panel shadow-xl shadow-black"
				role="region" aria-label="Data readiness" data-testid="readiness-panel">
				<div class="flex items-baseline gap-2 border-b border-sc-line px-3 py-1.5">
					<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Data</span>
					<span class="font-mono text-[11px] text-sc-ink">{report.subject.symbol} · {report.subject.timeframe}</span>
				</div>
				<p class="px-3 py-1.5 text-[11px] text-sc-ink" data-testid="readiness-summary">{report.summary}</p>
				<ul>
					{#each report.requirements as req (req.key)}
						{@const href = req.status === 'ok' ? null : readinessFixHref(req.fix)}
						<li class="border-t border-sc-line px-3 py-1.5 text-[11px]" data-testid="readiness-requirement" data-status={req.status}>
							<div class="flex items-baseline gap-2">
								<span class="w-[4.5rem] shrink-0 border px-1 text-center font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {STATUS[req.status].tone}">{STATUS[req.status].word}</span>
								<span class="text-sc-ink">{req.label}</span>
							</div>
							<div class="mt-0.5 pl-[5.25rem] text-[10px] leading-snug text-sc-ink2">{req.detail}</div>
							{#if href && req.fix}
								<a {href} class="mt-0.5 block pl-[5.25rem] text-[10px] text-sc-ink underline decoration-sc-line2 hover:decoration-sc-ink3">{req.fix.label} →</a>
							{/if}
						</li>
					{/each}
				</ul>
			</div>
		{/if}
	</div>
{:else if loading}
	<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink4" data-testid="readiness-loading">Checking data…</span>
{/if}
