<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import { fade, fly } from 'svelte/transition';
	import type { HelpContent } from '$lib/help/types';
	import { getHelpContent } from '$lib/help/content';

	export let helpId: string | null = null;
	export let isOpen = false;

	const dispatch = createEventDispatcher<{ close: void }>();

	let content: HelpContent | undefined;

	$: if (helpId) {
		content = getHelpContent(helpId);
	}

	function close() {
		isOpen = false;
		dispatch('close');
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') {
			close();
		}
	}

	function handleBackdropClick(e: MouseEvent) {
		if (e.target === e.currentTarget) {
			close();
		}
	}

	function getInterpretationColor(color: string): string {
		const colors: Record<string, string> = {
			red: 'bg-red-500/10 text-red-400 border-red-900',
			yellow: 'bg-yellow-500/10 text-yellow-400 border-yellow-900',
			green: 'bg-emerald-500/10 text-emerald-400 border-emerald-900',
			blue: 'bg-[#111] text-[#888] border-[#333]',
			gray: 'bg-[#111] text-[#888] border-[#333]'
		};
		return colors[color] || colors.gray;
	}

	function getCategoryLabel(category: string): string {
		const labels: Record<string, string> = {
			metric: 'Performance Metric',
			walkforward: 'Walk-Forward Analysis',
			optimization: 'Optimization',
			data: 'Data Concepts',
			strategy: 'Strategy Concepts',
			risk: 'Risk Management'
		};
		return labels[category] || category;
	}

	function getCategoryColor(category: string): string {
		const colors: Record<string, string> = {
			metric: 'border-[#333] text-[#888]',
			walkforward: 'border-[#333] text-[#888]',
			optimization: 'border-[#333] text-[#888]',
			data: 'border-[#333] text-[#888]',
			strategy: 'border-emerald-900 text-emerald-400',
			risk: 'border-red-900 text-red-400'
		};
		return colors[category] || 'border-[#333] text-[#888]';
	}

	onMount(() => {
		document.addEventListener('keydown', handleKeydown);
		return () => {
			document.removeEventListener('keydown', handleKeydown);
		};
	});
</script>

{#if isOpen && content}
	<!-- svelte-ignore a11y-click-events-have-key-events -->
	<!-- svelte-ignore a11y-no-static-element-interactions -->
	<div
		class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
		on:click={handleBackdropClick}
		transition:fade={{ duration: 150 }}
	>
		<div
			class="bg-[#050505] max-w-3xl w-full max-h-[90vh] overflow-hidden flex flex-col border border-[#222]"
			transition:fly={{ y: 20, duration: 200 }}
		>
			<!-- Header -->
			<div class="flex items-start justify-between p-4 border-b border-[#222]">
				<div>
					<div class="flex items-center gap-3 mb-2">
						<h2 class="text-lg font-bold uppercase tracking-widest text-white">{content.term}</h2>
						<span class="border px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest {getCategoryColor(content.category)}">
							{getCategoryLabel(content.category)}
						</span>
					</div>
					<p class="text-xs text-[#888]">{content.shortDescription}</p>
				</div>
				<button
					class="text-[#555] hover:text-white p-2 hover:bg-[#111] transition-colors"
					aria-label="Close help modal"
					title="Close help modal"
					on:click={close}
				>
					<svg class="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			<!-- Content -->
			<div class="flex-1 overflow-y-auto p-4 space-y-6">
				<!-- Full Description -->
				<section>
					<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
						<svg class="w-4 h-4 text-[#666]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
						</svg>
						Overview
					</h3>
					<div class="text-sm text-[#888] leading-relaxed whitespace-pre-line">
						{content.fullDescription}
					</div>
				</section>

				<!-- Formula -->
				{#if content.formula}
					<section>
						<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
							<svg class="w-4 h-4 text-[#666]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 7h6m0 10v-3m-3 3h.01M9 17h.01M9 14h.01M12 14h.01M15 11h.01M12 11h.01M9 11h.01M7 21h10a2 2 0 002-2V5a2 2 0 00-2-2H7a2 2 0 00-2 2v14a2 2 0 002 2z" />
							</svg>
							Formula
						</h3>
						<div class="bg-[#050505] p-4 border border-[#1a1a1a]">
							<code class="text-lg text-white font-mono block mb-4">{content.formula.plain}</code>
							<div class="space-y-2">
								<p class="text-xs text-[#666] font-medium mb-2">Where:</p>
								{#each Object.entries(content.formula.variables) as [variable, description]}
									<div class="flex gap-3 text-sm">
										<span class="text-white font-mono font-bold min-w-[80px]">{variable}</span>
										<span class="text-[#888]">= {description}</span>
									</div>
								{/each}
							</div>
						</div>
					</section>
				{/if}

				<!-- Interpretations -->
				{#if content.interpretations && content.interpretations.length > 0}
					<section>
						<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
							<svg class="w-4 h-4 text-[#666]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
							</svg>
							Interpretation Guide
						</h3>
						<div class="space-y-2">
							{#each content.interpretations as interp}
								<div class="flex items-start gap-3 p-3 border {getInterpretationColor(interp.color)}">
									<div class="min-w-[100px]">
										<span class="font-mono font-bold">{interp.range}</span>
									</div>
									<div class="min-w-[90px]">
										<span class="font-semibold">{interp.label}</span>
									</div>
									<div class="flex-1 text-sm opacity-90">
										{interp.description}
									</div>
								</div>
							{/each}
						</div>
					</section>
				{/if}

				<!-- Examples -->
				{#if content.examples && content.examples.length > 0}
					<section>
						<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
							<svg class="w-4 h-4 text-[#666]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
							</svg>
							Practical Examples
						</h3>
						<div class="space-y-4">
							{#each content.examples as example, i}
								<div class="bg-[#050505] p-4 border border-[#1a1a1a]">
									<div class="text-xs text-[#666] mb-2 uppercase tracking-wider">Example {i + 1}</div>
									<div class="space-y-3">
										<div>
											<span class="text-[10px] text-[#666] uppercase tracking-wide">Scenario</span>
											<p class="text-sm text-[#888]">{example.scenario}</p>
										</div>
										<div>
											<span class="text-[10px] text-[#666] uppercase tracking-wide">Calculation</span>
											<p class="text-[#888] font-mono">{example.calculation}</p>
										</div>
										<div>
											<span class="text-[10px] text-[#666] uppercase tracking-wide">Result</span>
											<p class="text-emerald-400 font-semibold">{example.result}</p>
										</div>
										<div>
											<span class="text-[10px] text-[#666] uppercase tracking-wide">Interpretation</span>
											<p class="text-sm text-[#888]">{example.interpretation}</p>
										</div>
									</div>
								</div>
							{/each}
						</div>
					</section>
				{/if}

				<!-- Limitations -->
				{#if content.limitations && content.limitations.length > 0}
					<section>
						<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
							<svg class="w-4 h-4 text-red-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
							</svg>
							Limitations & Pitfalls
						</h3>
						<ul class="space-y-2">
							{#each content.limitations as limitation}
								<li class="flex items-start gap-3 text-sm text-[#888]">
									<svg class="w-4 h-4 text-red-400 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
									</svg>
									{limitation}
								</li>
							{/each}
						</ul>
					</section>
				{/if}

				<!-- Pro Tips -->
				{#if content.proTips && content.proTips.length > 0}
					<section>
						<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
							<svg class="w-4 h-4 text-[#666]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" />
							</svg>
							Pro Tips
						</h3>
						<ul class="space-y-2">
							{#each content.proTips as tip}
								<li class="flex items-start gap-3 text-sm text-[#888]">
									<svg class="w-4 h-4 text-emerald-400 mt-0.5 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
										<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
									</svg>
									{tip}
								</li>
							{/each}
						</ul>
					</section>
				{/if}

				<!-- References -->
				{#if content.references && content.references.length > 0}
					<section>
						<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
							<svg class="w-4 h-4 text-[#666]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
							</svg>
							References
						</h3>
						<ul class="space-y-1 text-sm text-[#666]">
							{#each content.references as ref}
								<li class="italic">{ref}</li>
							{/each}
						</ul>
					</section>
				{/if}

				<!-- Related Terms -->
				{#if content.relatedTerms && content.relatedTerms.length > 0}
					<section>
						<h3 class="text-sm font-bold uppercase tracking-wider text-white mb-3 flex items-center gap-2">
							<svg class="w-4 h-4 text-[#666]" fill="none" viewBox="0 0 24 24" stroke="currentColor">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 20l4-16m2 16l4-16M6 9h14M4 15h14" />
							</svg>
							Related Terms
						</h3>
						<div class="flex flex-wrap gap-2">
							{#each content.relatedTerms as term}
								<button
									class="px-3 py-1 bg-[#111] hover:bg-[#222] text-[#888] hover:text-white text-xs transition-colors border border-[#333] hover:border-[#555]"
									on:click={() => {
										helpId = term;
									}}
								>
									{term.replace(/_/g, ' ')}
								</button>
							{/each}
						</div>
					</section>
				{/if}
			</div>

			<!-- Footer -->
			<div class="p-4 border-t border-[#222] bg-[#050505]">
				<div class="flex justify-between items-center">
					<span class="text-xs text-[#666]">Press ESC to close</span>
					<button
						class="terminal-button-primary text-xs"
						on:click={close}
					>
						Got it
					</button>
				</div>
			</div>
		</div>
	</div>
{/if}
