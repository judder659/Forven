<script lang="ts">
	/** Each strategy parameter against its declared search space. */
	export let params: Record<string, unknown> = {};
	export let space: Record<string, unknown> = {};

	const same = (a: unknown, b: unknown) => String(a) === String(b) || (Number(a) === Number(b) && Number.isFinite(Number(a)));
	$: rows = Object.entries(params)
		.filter(([key]) => !key.startsWith('_'))
		.sort(([a], [b]) => a.localeCompare(b))
		.map(([key, value]) => {
			const options = Array.isArray(space[key]) ? (space[key] as unknown[]) : null;
			const at = options ? options.findIndex((option) => same(option, value)) : -1;
			const note = !options
				? 'fixed (not searched)'
				: at === -1
					? 'outside the searched values'
					: options.length === 1
						? 'the only searched value'
						: at === 0
							? 'at the low edge of its range'
							: at === options.length - 1
								? 'at the high edge of its range'
								: 'inside its range';
			return { key, value, options, at, note, edge: options !== null && options.length > 1 && (at === 0 || at === options.length - 1) };
		});
	const show = (value: unknown) => (value && typeof value === 'object' ? JSON.stringify(value) : String(value ?? '—'));
</script>

<div class="grid" data-testid="parameter-space">
	{#each rows as row (row.key)}
		<div class="grid grid-cols-[minmax(0,11em)_minmax(0,1fr)] items-start gap-3 border-b border-[#161616] py-2 last:border-b-0" data-testid={`parameter-space-${row.key}`}>
			<span class="break-all font-mono text-[12px] text-[#aab1bc]">{row.key}</span>
			<div class="grid gap-1">
				<div class="flex flex-wrap gap-1">
					{#if row.options}
						{#each row.options as option, index (index)}
							<span class={`border px-1.5 py-0.5 font-mono text-[11px] ${index === row.at ? 'border-white bg-white text-black' : 'border-[#2a2f38] text-[#777]'}`}>{show(option)}</span>
						{/each}
						{#if row.at === -1}<span class="border border-[#e7b24a] px-1.5 py-0.5 font-mono text-[11px] text-[#e7b24a]">{show(row.value)}</span>{/if}
					{:else}
						<span class="border border-[#1d1d1d] px-1.5 py-0.5 font-mono text-[11px] text-white">{show(row.value)}</span>
					{/if}
				</div>
				<span class={`text-[11px] ${row.edge || (row.at === -1 && row.options) ? 'text-[#e7b24a]' : 'text-[#666]'}`}>{row.note}</span>
			</div>
		</div>
	{/each}
	{#if rows.length === 0}
		<div class="text-[12px] text-[#666]">No tunable parameters.</div>
	{/if}
</div>
