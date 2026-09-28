<script lang="ts">
	// Import a file: drop a CSV, check how its columns and times are read, see
	// the parsed bars in UTC with the timeframe the server infers, pick where
	// it goes and how it overlaps what is stored, then import. Nothing is
	// written before the last step.
	import { onDestroy } from 'svelte';
	import { commitImport, previewImport, type ImportParsing } from '$lib/api/dataManager';
	import type { ImportPreview, ImportResult } from '$lib/api/dataManagerTypes';
	import SectionState from '$lib/components/data-manager/SectionState.svelte';
	import { runAction } from '$lib/components/data-manager/actions';
	import { formatBytes, formatCount, formatPercent, formatUtc, venueLabel } from '$lib/components/data-manager/format';
	import { DM, seriesHref } from '$lib/components/data-manager/links';
	import { defaultImportMode, normalizeSymbol, validateImport, type ImportDraft } from '$lib/components/data-manager/wizard';
	import { createRequestGuard, loading, settle, type Loadable } from '$lib/stores/dataManager';

	const ROLES = ['timestamp', 'open', 'high', 'low', 'close', 'volume'] as const;
	const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '1w'];
	const ZONES = ['UTC', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Chicago', 'America/Sao_Paulo', 'Asia/Tokyo', 'Asia/Hong_Kong', 'Asia/Singapore', 'Australia/Sydney'];

	let file: File | null = null;
	let dragging = false;
	let fileInput: HTMLInputElement | undefined;
	let mapping: Record<string, string | null> = {};
	let dateFormat = '';
	let timezone = 'UTC';
	let preview: Loadable<ImportPreview> | null = null;
	let draft: ImportDraft = { symbol: '', timeframe: '', mode: 'new', conflict_policy: 'keep_existing' };
	let modeTouched = false;
	let result: ImportResult | null = null;
	let importing = false;
	let checking = false;
	let timer: ReturnType<typeof setTimeout> | undefined;
	const guard = createRequestGuard();
	onDestroy(() => {
		clearTimeout(timer);
		guard.cancel();
	});

	function parsing(): ImportParsing {
		return {
			symbol: draft.symbol.trim() ? normalizeSymbol(draft.symbol) : undefined,
			timeframe: draft.timeframe || undefined,
			timezone,
			date_format: dateFormat.trim() || undefined,
			mapping: Object.keys(mapping).length ? mapping : undefined,
		};
	}

	const readKey = (d: ImportDraft, m: Record<string, string | null>, zone: string, format: string) =>
		JSON.stringify([d.symbol.trim() ? normalizeSymbol(d.symbol) : '', d.timeframe, m, zone, format]);

	async function read() {
		if (!file) return;
		checkedKey = readKey(draft, mapping, timezone, dateFormat);
		const { current } = guard.next();
		preview = preview?.data ? { ...preview, status: 'ready' } : loading();
		checking = true;
		const next = await settle(previewImport(file, parsing()), preview);
		if (!current()) return;
		checking = false;
		preview = next;
		const p = next.data;
		if (!p) return;
		if (!Object.keys(mapping).length) mapping = { ...p.mapping };
		if (!draft.timeframe && p.inferred_timeframe) draft = { ...draft, timeframe: p.inferred_timeframe };
		if (!modeTouched) draft = { ...draft, mode: defaultImportMode(p) };
	}

	function choose(next: File | null | undefined) {
		if (!next) return;
		file = next;
		result = null;
		mapping = {};
		preview = null;
		draft = { ...draft, timeframe: '' };
		modeTouched = false;
		void read();
	}
	function onDrop(event: DragEvent) {
		event.preventDefault();
		dragging = false;
		choose(event.dataTransfer?.files?.[0]);
	}

	// Re-check (debounced) when the reading options or the target change.
	let checkedKey = '';
	$: checkKey = readKey(draft, mapping, timezone, dateFormat);
	$: if (file && preview?.data && checkKey !== checkedKey && !checking) {
		clearTimeout(timer);
		timer = setTimeout(read, 450);
	}

	$: p = preview?.data ?? null;
	$: target = p?.target ?? null;
	$: verdict = validateImport(p, draft);
	$: fresh = !checking && checkKey === checkedKey;
	$: sampleColumns = p?.columns ?? [];

	async function doImport() {
		if (!file || !p) return;
		importing = true;
		const { symbol: _s, timeframe: _t, ...options } = parsing();
		const done = await runAction(
			'Importing',
			() => commitImport(file!, { symbol: normalizeSymbol(draft.symbol), timeframe: draft.timeframe, mode: draft.mode, conflict_policy: draft.conflict_policy }, options),
			{ success: (r) => `Imported ${formatCount(r.rows_written)} rows into ${r.series.symbol} ${r.series.timeframe}.`, poke: false },
		);
		importing = false;
		if (done) result = done;
	}

	function reset() {
		file = null;
		preview = null;
		result = null;
		mapping = {};
		draft = { symbol: '', timeframe: '', mode: 'new', conflict_policy: 'keep_existing' };
		if (fileInput) fileInput.value = '';
	}
</script>

<svelte:head><title>Data · Import file | Forven</title></svelte:head>

<div class="mx-auto max-w-5xl space-y-3 p-4 pb-24">
	<div class="flex items-center justify-between gap-3">
		<h1 class="text-[11px] font-bold uppercase tracking-[0.2em] text-white">Import a file</h1>
		<a href="{DM}/get" class="text-[10px] uppercase tracking-wider text-[#777] hover:text-white">Download from a venue instead →</a>
	</div>

	{#if result}
		<section class="border border-emerald-900/70 bg-emerald-500/[0.03] px-4 py-3" aria-live="polite">
			<h2 class="text-[14px] font-bold text-emerald-400">Imported {formatCount(result.rows_written)} rows into {result.series.symbol} {result.series.timeframe}</h2>
			<p class="mt-1 text-[11px] text-[#aaa]">
				{venueLabel(result.series.venue)} · {formatCount(result.new_bars)} new bars{#if result.overwritten} · {formatCount(result.overwritten)} replaced{/if}{#if result.kept} · {formatCount(result.kept)} stored bars kept{/if}
			</p>
			{#each result.warnings as warning}<p class="text-[11px] text-amber-400">{warning}</p>{/each}
			<div class="mt-3 flex gap-2">
				<a href={seriesHref(result.series)} class="terminal-button-primary text-[10px]">Open the series</a>
				<button type="button" class="terminal-button text-[10px]" on:click={reset}>Import another file</button>
			</div>
		</section>
	{:else}
		<!-- 1 File -->
		<section aria-label="File">
			<label on:dragover|preventDefault={() => (dragging = true)} on:dragleave={() => (dragging = false)} on:drop={onDrop}
				class="flex cursor-pointer flex-col items-center justify-center gap-1 border border-dashed px-6 py-8 text-center transition-colors {dragging ? 'border-white bg-white/[0.04]' : 'border-[#333] bg-[#050505] hover:border-[#666]'}">
				<input bind:this={fileInput} type="file" accept=".csv,.txt,text/csv" class="sr-only" on:change={(e) => choose(e.currentTarget.files?.[0])} />
				{#if file}
					<span class="text-[13px] text-white">{file.name}</span>
					<span class="text-[11px] text-[#777]">{formatBytes(file.size)} · drop another file or click to replace it</span>
				{:else}
					<span class="text-[13px] text-white">Drop a CSV here, or click to choose one</span>
					<span class="text-[11px] text-[#777]">One row per bar: a time column plus open, high, low, close (volume optional). Nothing is written until you import.</span>
				{/if}
			</label>
		</section>

		{#if preview}
			<SectionState state={preview} what="The file check" endpoint="POST /api/data/acquire/import/preview" rows={5} on:retry={read}>
				{#if p}
					<div class="grid gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
						<div class="min-w-0 space-y-3">
							<!-- 2 Columns and time -->
							<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-imp-cols">
								<header class="flex items-center gap-2 border-b border-[#141414] px-3 py-1.5">
									<h2 id="dm-imp-cols" class="text-[11px] font-bold uppercase tracking-wider text-white">How the file is read</h2>
									<span class="text-[10px] text-[#666]">{formatCount(p.rows)} rows · {p.columns.length} columns</span>
									{#if checking}<span class="ml-auto text-[10px] text-[#888]">checking…</span>{/if}
								</header>
								<div class="grid gap-2 px-3 py-2.5 sm:grid-cols-3">
									{#each ROLES as role}
										<label class="text-[10px] uppercase tracking-wider text-[#666]">{role}{role === 'volume' ? ' (optional)' : ''}
											<select value={mapping[role] ?? ''} on:change={(e) => (mapping = { ...mapping, [role]: e.currentTarget.value || null })}
												class="mt-1 w-full border bg-black px-1.5 py-1 text-[11px] normal-case tracking-normal text-[#ddd] outline-none focus:border-white {!mapping[role] && role !== 'volume' ? 'border-amber-800' : 'border-[#2a2a2a]'}">
												<option value="">— not in the file —</option>
												{#each p.columns as column}<option value={column}>{column}</option>{/each}
											</select>
										</label>
									{/each}
									<label class="text-[10px] uppercase tracking-wider text-[#666]">Time format
										<input bind:value={dateFormat} placeholder="detect automatically" spellcheck="false"
											class="mt-1 w-full border border-[#2a2a2a] bg-black px-1.5 py-1 font-mono text-[11px] normal-case tracking-normal text-[#ddd] outline-none placeholder:text-[#555] focus:border-white"
											title="Python strftime format, e.g. %Y.%m.%d %H:%M; leave empty to detect it" />
									</label>
									<label class="text-[10px] uppercase tracking-wider text-[#666]">Time zone of naive times
										<select bind:value={timezone} class="mt-1 w-full border border-[#2a2a2a] bg-black px-1.5 py-1 text-[11px] normal-case tracking-normal text-[#ddd] outline-none focus:border-white">
											{#each ZONES as zone}<option value={zone}>{zone}</option>{/each}
										</select>
									</label>
								</div>
							</section>

							<!-- What the file holds -->
							<section class="border border-[#222] bg-[#050505]" aria-labelledby="dm-imp-parsed">
								<header class="border-b border-[#141414] px-3 py-1.5"><h2 id="dm-imp-parsed" class="text-[11px] font-bold uppercase tracking-wider text-white">What it holds</h2></header>
								<dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 px-3 py-2 text-[11px]">
									<dt class="text-[#666]">Span</dt><dd class="text-[#ddd]">{p.first_ts ? `${formatUtc(p.first_ts)} → ${formatUtc(p.last_ts)}` : '—'}</dd>
									<dt class="text-[#666]">Bars look</dt>
									<dd class="text-[#ddd]">{#if p.inferred_timeframe}<span class="font-mono text-white">{p.inferred_timeframe}</span> apart <span class="text-[#888]">({formatPercent(p.timeframe_confidence, 0)} of consecutive rows)</span>{:else}irregular: no single spacing{/if}</dd>
									<dt class="text-[#666]">Off the grid</dt><dd class={p.misaligned_rows ? 'text-amber-400' : 'text-[#888]'}>{formatCount(p.misaligned_rows)} rows not on a bar boundary</dd>
									<dt class="text-[#666]">Unreadable</dt><dd class={p.invalid_rows ? 'text-amber-400' : 'text-[#888]'}>{formatCount(p.invalid_rows)} rows</dd>
								</dl>
								{#each p.errors as error}<p class="px-3 pb-1 text-[11px] text-red-400">{error}</p>{/each}
								{#each p.warnings as warning}<p class="px-3 pb-1 text-[11px] text-amber-400">{warning}</p>{/each}
								<div class="grid gap-2 border-t border-[#141414] px-3 py-2 xl:grid-cols-2">
									<div class="min-w-0">
										<div class="text-[9px] uppercase tracking-wider text-[#555]">In the file</div>
										<div class="overflow-x-auto">
											<table class="mt-1 w-full text-[10px]">
												<thead><tr class="text-[#666]">{#each sampleColumns as column}<th class="whitespace-nowrap px-1.5 py-0.5 text-left font-normal">{column}</th>{/each}</tr></thead>
												<tbody>{#each p.sample as row, i (i)}<tr class="border-t border-[#111]">{#each sampleColumns as column}<td class="whitespace-nowrap px-1.5 py-0.5 font-mono text-[#bbb]">{String(row[column] ?? '')}</td>{/each}</tr>{/each}</tbody>
											</table>
										</div>
									</div>
									<div class="min-w-0">
										<div class="text-[9px] uppercase tracking-wider text-[#555]">As it will be stored (UTC)</div>
										<div class="overflow-x-auto">
											<table class="mt-1 w-full text-[10px]">
												<thead><tr class="text-[#666]"><th class="px-1.5 py-0.5 text-left font-normal">time</th><th class="px-1.5 py-0.5 text-right font-normal">open</th><th class="px-1.5 py-0.5 text-right font-normal">high</th><th class="px-1.5 py-0.5 text-right font-normal">low</th><th class="px-1.5 py-0.5 text-right font-normal">close</th><th class="px-1.5 py-0.5 text-right font-normal">vol</th></tr></thead>
												<tbody>
													{#each p.parsed_sample as bar (bar.t)}
														<tr class="border-t border-[#111] font-mono text-[#ddd]">
															<td class="whitespace-nowrap px-1.5 py-0.5">{formatUtc(bar.t, { suffix: false })}</td>
															<td class="px-1.5 py-0.5 text-right">{bar.o}</td><td class="px-1.5 py-0.5 text-right">{bar.h}</td><td class="px-1.5 py-0.5 text-right">{bar.l}</td><td class="px-1.5 py-0.5 text-right">{bar.c}</td><td class="px-1.5 py-0.5 text-right">{bar.v}</td>
														</tr>
													{/each}
												</tbody>
											</table>
										</div>
									</div>
								</div>
							</section>
						</div>

						<!-- 3 Target and 4 Import -->
						<aside class="min-w-0">
							<section class="border border-[#222] bg-[#050505] lg:sticky lg:top-3" aria-labelledby="dm-imp-target">
								<header class="border-b border-[#141414] px-3 py-1.5"><h2 id="dm-imp-target" class="text-[11px] font-bold uppercase tracking-wider text-white">Where it goes</h2></header>
								<div class="space-y-2.5 px-3 py-2.5 text-[11px]">
									<label class="block text-[10px] uppercase tracking-wider text-[#666]">Symbol
										<input value={draft.symbol} on:input={(e) => (draft = { ...draft, symbol: e.currentTarget.value })} placeholder="e.g. XAU-USD" spellcheck="false"
											class="terminal-input mt-1 font-mono text-[12px] normal-case tracking-normal" /></label>
									<label class="block text-[10px] uppercase tracking-wider text-[#666]">Timeframe
										<select value={draft.timeframe} on:change={(e) => (draft = { ...draft, timeframe: e.currentTarget.value })}
											class="mt-1 w-full border border-[#2a2a2a] bg-black px-1.5 py-1 font-mono text-[12px] text-white outline-none focus:border-white">
											<option value="">—</option>
											{#each TIMEFRAMES as tf}<option value={tf}>{tf}{tf === p.inferred_timeframe ? ' (from the file)' : ''}</option>{/each}
										</select></label>
									{#if target && fresh}
										<div class="border border-[#1a1a1a] bg-black/40 p-2">
											<p class="text-[#ddd]">
												{#if target.exists}<span class="font-mono">{target.symbol} {target.timeframe}</span> is stored: {formatCount(target.existing_rows)} bars{target.existing_source ? ` from ${target.existing_source}` : ''}.
												{:else}<span class="font-mono">{target.symbol} {target.timeframe}</span> is not stored yet.{/if}
											</p>
											<p class="mt-1 text-[10px] {target.destination === 'canonical' ? 'text-emerald-400' : 'text-sky-300'}">
												{target.destination === 'canonical' ? 'Goes into the research series.' : 'Stored as a separate venue series (CSV); the research series is not touched.'}</p>
											{#if target.exists}
												<div class="mt-2 grid grid-cols-3 gap-1 text-center">
													<div class="border border-[#1a1a1a] py-1"><div class="font-mono text-white">{formatCount(target.overlap.new_bars)}</div><div class="text-[9px] uppercase text-[#666]">new</div></div>
													<div class="border border-[#1a1a1a] py-1"><div class="font-mono text-white">{formatCount(target.overlap.identical)}</div><div class="text-[9px] uppercase text-[#666]">identical</div></div>
													<div class="border py-1 {target.overlap.conflicting ? 'border-amber-900' : 'border-[#1a1a1a]'}"><div class="font-mono {target.overlap.conflicting ? 'text-amber-400' : 'text-white'}">{formatCount(target.overlap.conflicting)}</div><div class="text-[9px] uppercase text-[#666]">differ</div></div>
												</div>
												{#if target.overlap.conflict_examples.length}
													<table class="mt-1.5 w-full text-[10px]">
														<thead><tr class="text-[#666]"><th class="text-left font-normal">bar (UTC)</th><th class="text-right font-normal">stored close</th><th class="text-right font-normal">file close</th></tr></thead>
														<tbody>{#each target.overlap.conflict_examples as ex (ex.t)}<tr class="font-mono text-[#bbb]"><td>{formatUtc(ex.t, { suffix: false })}</td><td class="text-right">{ex.stored_close}</td><td class="text-right text-amber-300">{ex.file_close}</td></tr>{/each}</tbody>
													</table>
												{/if}
											{/if}
										</div>
										<fieldset class="space-y-1">
											<legend class="text-[10px] uppercase tracking-wider text-[#666]">Import as</legend>
											<label class="flex items-center gap-1.5 text-[#ccc]"><input type="radio" name="dm-imp-mode" value="new" checked={draft.mode === 'new'} on:change={() => { modeTouched = true; draft = { ...draft, mode: 'new' }; }} class="accent-white" /> New series</label>
											<label class="flex items-center gap-1.5 text-[#ccc]"><input type="radio" name="dm-imp-mode" value="patch" checked={draft.mode === 'patch'} on:change={() => { modeTouched = true; draft = { ...draft, mode: 'patch' }; }} class="accent-white" /> Add bars to it (patch)</label>
										</fieldset>
										{#if draft.mode === 'patch' && target.exists}
											<fieldset class="space-y-1">
												<legend class="text-[10px] uppercase tracking-wider text-[#666]">Where they differ</legend>
												<label class="flex items-center gap-1.5 text-[#ccc]"><input type="radio" name="dm-imp-policy" checked={draft.conflict_policy === 'keep_existing'} on:change={() => (draft = { ...draft, conflict_policy: 'keep_existing' })} class="accent-white" /> Keep the stored values</label>
												<label class="flex items-center gap-1.5 text-[#ccc]"><input type="radio" name="dm-imp-policy" checked={draft.conflict_policy === 'overwrite'} on:change={() => (draft = { ...draft, conflict_policy: 'overwrite' })} class="accent-white" /> Use the file’s values</label>
											</fieldset>
										{/if}
									{:else if draft.symbol.trim() && draft.timeframe}
										<p class="text-[#666]">Checking {normalizeSymbol(draft.symbol)} {draft.timeframe}…</p>
									{/if}
									{#if verdict.errors.length && (fresh || !draft.symbol.trim() || !draft.timeframe)}
										<ul class="space-y-0.5 text-[#999]">{#each verdict.errors as error}<li>· {error}</li>{/each}</ul>
									{/if}
									{#each verdict.warnings as warning}<p class="text-amber-400">{warning}</p>{/each}
									<button type="button" class="terminal-button-primary w-full text-[10px]" disabled={!!verdict.errors.length || !fresh || importing} on:click={doImport}>
										{importing ? 'Importing…' : 'Import'}</button>
								</div>
							</section>
						</aside>
					</div>
				{/if}
			</SectionState>
		{/if}
	{/if}
</div>
