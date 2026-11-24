<script context="module" lang="ts">
	export interface WizardRailStep {
		id: string;
		label: string;
		subtitle: string;
		status: 'complete' | 'recommended' | 'pending';
	}
</script>

<script lang="ts">
	export let steps: WizardRailStep[];
	export let activeIndex: number;
	export let onSelect: (index: number) => void;
	export let onSkipAll: () => void;

	$: progressPct = steps.length > 1 ? Math.round(((activeIndex + 1) / steps.length) * 100) : 100;

	function statusSuffix(step: WizardRailStep): string {
		if (step.status === 'complete') return ' (complete)';
		if (step.status === 'recommended') return ' (recommended)';
		return ' (upcoming)';
	}
</script>

<nav
	aria-label="Setup guide steps"
	class="flex flex-col h-full border-r border-sc-line bg-sc-panel w-[248px] flex-shrink-0 p-4 select-none"
>
	<div class="pb-4 mb-3 border-b border-sc-line">
		<div class="flex items-center justify-between gap-2">
			<span class="font-plex-cond text-[11px] font-semibold uppercase tracking-[0.12em] text-sc-ink3">
				Forven Setup Guide
			</span>
			<span class="font-plex-mono text-[11px] text-sc-ink3">
				{activeIndex + 1}/{steps.length}
			</span>
		</div>
		<div class="mt-2 h-1 w-full rounded-full bg-sc-raise overflow-hidden" aria-hidden="true">
			<div
				class="h-full rounded-full bg-emerald-400/90 transition-all duration-300"
				style="width: {progressPct}%"
			></div>
		</div>
	</div>

	<ol class="flex-1 space-y-1">
		{#each steps as step, i (step.id)}
			{@const isActive = i === activeIndex}
			<li>
				<button
					type="button"
					aria-current={isActive ? 'step' : undefined}
					aria-label={`${i + 1}. ${step.label}${statusSuffix(step)}`}
					class="w-full flex items-start gap-3 text-left px-2.5 py-2.5 rounded-md border transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-sc-ink3
						{isActive
							? 'border-sc-line2 bg-sc-panel2 text-sc-ink'
							: 'border-transparent text-sc-ink2 hover:bg-sc-panel2/60 hover:text-sc-ink'}"
					on:click={() => onSelect(i)}
				>
					<span
						class="mt-0.5 w-5 h-5 rounded-full flex items-center justify-center flex-shrink-0 text-[11px] font-medium border
							{step.status === 'complete'
								? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-400'
								: isActive
									? 'border-sc-ink text-sc-ink bg-sc-raise'
									: step.status === 'recommended'
										? 'border-amber-400/40 bg-amber-400/10 text-amber-300'
										: 'border-sc-line2 text-sc-ink3 bg-sc-bg'}"
						aria-hidden="true"
					>
						{#if step.status === 'complete'}
							✓
						{:else}
							{i + 1}
						{/if}
					</span>
					<span class="min-w-0 flex-1">
						<span class="block text-[12.5px] font-medium leading-snug truncate {isActive ? 'text-sc-ink' : 'text-sc-ink2'}">
							{step.label}
						</span>
						<span class="block text-[11px] leading-snug text-sc-ink3 truncate mt-0.5">
							{step.subtitle}
						</span>
					</span>
				</button>
			</li>
		{/each}
	</ol>

	<div class="pt-3 mt-3 border-t border-sc-line space-y-2">
		<p class="text-[11px] leading-relaxed text-sc-ink3 px-1">
			Nothing trades real money unless you explicitly connect &amp; arm an exchange.
		</p>
		<button
			type="button"
			class="w-full text-[11.5px] text-sc-ink3 hover:text-sc-ink text-left px-1 py-1 rounded transition-colors focus:outline-none"
			on:click={onSkipAll}
		>
			Skip setup — I’ll explore on my own
		</button>
	</div>
</nav>
