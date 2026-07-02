<script lang="ts">
	import {
		runEvidenceCleanup,
		runTriageBatch,
		type EvidenceCleanupResponse,
		type TriageBatchResponse,
	} from '$lib/api';

	type LogTone = 'info' | 'success' | 'error';

	interface LogEntry {
		id: number;
		timestamp: string;
		tone: LogTone;
		message: string;
	}

	let logs: LogEntry[] = [];
	let logCounter = 0;
	let evidenceBusy = false;
	let triageBusy = false;
	let dryRun = false;
	let triageBatchSize = 10;
	let evidenceResult: EvidenceCleanupResponse | null = null;
	let evidenceWasDryRun = false;
	let triageResult: TriageBatchResponse | null = null;

	function pushLog(tone: LogTone, message: string): void {
		logCounter += 1;
		logs = [
			{ id: logCounter, timestamp: new Date().toLocaleTimeString(), tone, message },
			...logs,
		].slice(0, 50);
	}

	async function doEvidenceCleanup(): Promise<void> {
		if (evidenceBusy) return;
		if (!dryRun && !confirm('Run the evidence rule for real? This permanently marks matching hypotheses disproven.')) {
			return;
		}
		evidenceBusy = true;
		evidenceWasDryRun = dryRun;
		pushLog('info', `Running evidence rule${dryRun ? ' (dry run)' : ''}…`);
		try {
			const result: EvidenceCleanupResponse = await runEvidenceCleanup(dryRun);
			evidenceResult = result;
			const count = dryRun ? (result.would_disprove_count ?? 0) : (result.disproven_count ?? 0);
			const label = dryRun ? 'would disprove' : 'disproved';
			pushLog('success', `Evidence rule: ${label} ${count} hypotheses.`);
		} catch (err) {
			pushLog('error', `Evidence rule failed: ${err instanceof Error ? err.message : String(err)}`);
		} finally {
			evidenceBusy = false;
		}
	}

	async function doTriage(): Promise<void> {
		if (triageBusy) return;
		triageBusy = true;
		pushLog('info', `Running LLM triage (batch size ${triageBatchSize})…`);
		try {
			const result: TriageBatchResponse = await runTriageBatch(triageBatchSize);
			triageResult = result;
			pushLog(
				'success',
				`Triage: wrote ${result.processed_count} memos, ${result.errors?.length ?? 0} errors.`,
			);
			if (result.errors?.length) {
				for (const entry of result.errors) {
					pushLog('error', `  - ${entry.id}: ${entry.error_code ?? 'unknown_error'}`);
				}
			}
		} catch (err) {
			pushLog('error', `Triage failed: ${err instanceof Error ? err.message : String(err)}`);
		} finally {
			triageBusy = false;
		}
	}

	function toneClass(tone: LogTone): string {
		if (tone === 'error') return 'text-red-400';
		if (tone === 'success') return 'text-emerald-400';
		return 'text-[#888]';
	}
</script>

<section class="mx-auto max-w-4xl px-4 py-6">
	<header class="mb-4 border-b border-[#222] pb-4">
		<h1 class="text-lg font-bold uppercase tracking-widest text-white">Crucible cleanup</h1>
		<p class="mt-1 text-xs text-[#666]">
			Run evidence-rule sweeps and LLM triage batches over hypotheses that still need verdict memos.
		</p>
	</header>

	<div class="grid gap-4 md:grid-cols-2">
		<div class="terminal-card p-4">
			<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Evidence rule</h2>
			<p class="mt-1 text-xs text-[#666]">
				Mark any hypothesis disproven whose best strategy has ≥ 3 fails and 0 passes.
			</p>
			<label class="mt-3 inline-flex items-center gap-2 text-xs text-[#888]">
				<input type="checkbox" bind:checked={dryRun} class="border-[#333] accent-white" />
				Dry run (preview only)
			</label>
			<button
				type="button"
				class="terminal-button-primary mt-3 w-full text-xs disabled:opacity-50"
				on:click={doEvidenceCleanup}
				disabled={evidenceBusy}
			>
				{evidenceBusy ? 'Running…' : 'Run evidence rule'}
			</button>
			{#if evidenceResult}
				{@const ids = evidenceResult.ids ?? []}
				{@const count = evidenceWasDryRun
					? (evidenceResult.would_disprove_count ?? ids.length)
					: (evidenceResult.disproven_count ?? ids.length)}
				<div class="mt-3 border border-[#222] bg-black p-3 text-xs text-[#888]">
					<div class="font-semibold text-[#888]">
						{evidenceWasDryRun ? `Would disprove ${count}` : `Disproved ${count}`} hypotheses
					</div>
					{#if ids.length}
						<ul class="mt-2 flex flex-wrap gap-x-3 gap-y-1">
							{#each ids as id (id)}
								<li>
									<a href={`/hypotheses/${id}`} class="text-white underline-offset-2 hover:underline">
										{id}
									</a>
								</li>
							{/each}
						</ul>
					{:else}
						<p class="mt-1 text-[#666]">No matching hypotheses.</p>
					{/if}
				</div>
			{/if}
		</div>

		<div class="terminal-card p-4">
			<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">LLM triage</h2>
			<p class="mt-1 text-xs text-[#666]">
				Write a verdict memo for each hypothesis still missing one.
			</p>
			<label class="mt-3 flex items-center gap-2 text-xs text-[#888]">
				Batch size
				<input
					type="number"
					bind:value={triageBatchSize}
					min="1"
					max="50"
					class="terminal-input w-20 text-xs"
				/>
			</label>
			<button
				type="button"
				class="terminal-button-primary mt-3 w-full text-xs disabled:opacity-50"
				on:click={doTriage}
				disabled={triageBusy}
			>
				{triageBusy ? 'Running…' : 'Run LLM triage'}
			</button>
			{#if triageResult}
				{@const processedIds = triageResult.processed_ids ?? []}
				<div class="mt-3 border border-[#222] bg-black p-3 text-xs text-[#888]">
					<div class="font-semibold text-[#888]">
						Wrote {triageResult.processed_count} memos · {triageResult.errors?.length ?? 0} errors
					</div>
					{#if processedIds.length}
						<ul class="mt-2 flex flex-wrap gap-x-3 gap-y-1">
							{#each processedIds as id (id)}
								<li>
									<a href={`/hypotheses/${id}`} class="text-white underline-offset-2 hover:underline">
										{id}
									</a>
								</li>
							{/each}
						</ul>
					{:else}
						<p class="mt-1 text-[#666]">No hypotheses processed.</p>
					{/if}
				</div>
			{/if}
		</div>
	</div>

	<div class="terminal-card mt-6 p-4">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Activity log</h2>
		{#if logs.length === 0}
			<p class="mt-2 text-xs text-[#666]">No actions yet.</p>
		{:else}
			<ul class="mt-2 space-y-1 font-mono text-xs">
				{#each logs as entry (entry.id)}
					<li class={toneClass(entry.tone)}>
						<span class="text-[#555]">[{entry.timestamp}]</span>
						{entry.message}
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</section>
