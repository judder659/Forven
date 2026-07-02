<script lang="ts">
	import { fly } from 'svelte/transition';
	import { goto } from '$app/navigation';
	import { toasts, dismissToast, snoozeUntil, snoozeNotifications, clearSnooze, getSnoozeOptions } from '$lib/stores/processTracker';
	import type { ToastItem } from '$lib/stores/processTracker';
	import { onDestroy } from 'svelte';

	// Auto-dismiss timers
	const timers = new Map<string, ReturnType<typeof setTimeout>>();

	let showSnoozeMenu = false;
	let snoozeMenuRef: HTMLDivElement | null = null;

	function startTimer(t: ToastItem) {
		if (timers.has(t.id)) return;
		timers.set(
			t.id,
			setTimeout(() => {
				dismissToast(t.id);
				timers.delete(t.id);
			}, t.duration)
		);
	}

	function clearTimer(id: string) {
		const timer = timers.get(id);
		if (timer) {
			clearTimeout(timer);
			timers.delete(id);
		}
	}

	$: $toasts.forEach(startTimer);

	onDestroy(() => {
		timers.forEach((timer) => clearTimeout(timer));
		timers.clear();
	});

	function handleClick(t: ToastItem) {
		clearTimer(t.id);
		dismissToast(t.id);
		if (t.href) goto(t.href);
	}

	function borderColor(type: ToastItem['type']): string {
		switch (type) {
			case 'success': return 'border-emerald-900';
			case 'error': return 'border-red-900';
			case 'warning': return 'border-yellow-900';
			default: return 'border-[#333]';
		}
	}

	function formatSnoozeRemaining(ms: number): string {
		const minutes = Math.ceil(ms / 60000);
		if (minutes < 60) return `${minutes}m`;
		const hours = Math.floor(minutes / 60);
		const mins = minutes % 60;
		if (mins === 0) return `${hours}h`;
		return `${hours}h ${mins}m`;
	}

	function handleSnooze(durationMs: number) {
		snoozeNotifications(durationMs);
		showSnoozeMenu = false;
	}

	function handleClearSnooze() {
		clearSnooze();
	}

	// Close menu when clicking outside
	function handleClickOutside(event: MouseEvent) {
		if (snoozeMenuRef && !snoozeMenuRef.contains(event.target as Node)) {
			showSnoozeMenu = false;
		}
	}

	$: if (typeof window !== 'undefined') {
		if (showSnoozeMenu) {
			window.addEventListener('click', handleClickOutside, true);
		} else {
			window.removeEventListener('click', handleClickOutside, true);
		}
	}

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('click', handleClickOutside, true);
		}
	});

	const snoozeOptions = getSnoozeOptions();
</script>

{#if $toasts.length > 0}
	<div class="flex flex-col items-end gap-2 pointer-events-none">
		{#each $toasts as t (t.id)}
			<div
				class="pointer-events-auto bg-[#050505] border {borderColor(t.type)} px-4 py-3 min-w-[260px] max-w-sm text-left flex items-start gap-3 group cursor-pointer hover:bg-[#111] transition-colors"
				transition:fly={{ x: 300, duration: 250 }}
				role="button"
				tabindex="0"
				on:click={() => handleClick(t)}
				on:keydown={(event) => {
					if (event.key === 'Enter' || event.key === ' ') {
						event.preventDefault();
						handleClick(t);
					}
				}}
			>
				<!-- Icon -->
				<div class="flex-shrink-0 mt-0.5">
					{#if t.type === 'success'}
						<svg class="w-4 h-4 text-emerald-400" viewBox="0 0 20 20" fill="currentColor">
							<path fill-rule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clip-rule="evenodd" />
						</svg>
					{:else if t.type === 'error'}
						<svg class="w-4 h-4 text-red-500" viewBox="0 0 20 20" fill="currentColor">
							<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
						</svg>
					{:else if t.type === 'warning'}
						<svg class="w-4 h-4 text-yellow-400" viewBox="0 0 20 20" fill="currentColor">
							<path fill-rule="evenodd" d="M8.257 3.099c.765-1.36 2.722-1.36 3.486 0l5.58 9.92c.75 1.334-.213 2.98-1.742 2.98H4.42c-1.53 0-2.493-1.646-1.743-2.98l5.58-9.92zM11 13a1 1 0 11-2 0 1 1 0 012 0zm-1-8a1 1 0 00-1 1v3a1 1 0 002 0V6a1 1 0 00-1-1z" clip-rule="evenodd" />
						</svg>
					{:else}
						<svg class="w-4 h-4 text-[#888]" viewBox="0 0 20 20" fill="currentColor">
							<path fill-rule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z" clip-rule="evenodd" />
						</svg>
					{/if}
				</div>

				<!-- Content -->
				<div class="flex-1 min-w-0">
					<div class="text-xs text-white font-mono truncate">{t.message}</div>
					{#if t.href}
						<div class="text-[10px] text-[#666] mt-0.5 uppercase tracking-wider group-hover:text-[#999] transition-colors">Click to view</div>
					{/if}
				</div>

				<!-- Dismiss X -->
				<button
					type="button"
					aria-label="Dismiss notification"
					class="flex-shrink-0 text-[#555] hover:text-white transition-colors mt-0.5"
					on:click|stopPropagation={() => { clearTimer(t.id); dismissToast(t.id); }}
				>
					<svg class="w-3 h-3" viewBox="0 0 20 20" fill="currentColor">
						<path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" />
					</svg>
				</button>
			</div>
		{/each}

		<!-- Snooze Controls -->
		{#if $snoozeUntil > Date.now()}
			<!-- Snooze Active Indicator -->
			<div class="pointer-events-auto bg-[#050505] border border-[#333] px-3 py-2 flex items-center gap-2">
				<svg class="w-3 h-3 text-yellow-400" viewBox="0 0 20 20" fill="currentColor">
					<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
				</svg>
				<span class="text-[10px] text-[#888]">
					Snoozed for {formatSnoozeRemaining($snoozeUntil - Date.now())}
				</span>
				<button
					class="text-[10px] text-[#666] hover:text-white ml-2"
					on:click={handleClearSnooze}
				>
					Resume
				</button>
			</div>
		{:else}
			<!-- Snooze Button with Dropdown -->
			<div class="pointer-events-auto relative" bind:this={snoozeMenuRef}>
				<button
					class="bg-[#050505] border border-[#333] px-3 py-2 flex items-center gap-2 hover:bg-[#111] transition-colors"
					on:click|stopPropagation={() => showSnoozeMenu = !showSnoozeMenu}
					title="Snooze notifications"
				>
					<svg class="w-3 h-3 text-[#888]" viewBox="0 0 20 20" fill="currentColor">
						<path fill-rule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-12a1 1 0 10-2 0v4a1 1 0 00.293.707l2.828 2.829a1 1 0 101.415-1.415L11 9.586V6z" clip-rule="evenodd" />
					</svg>
					<span class="text-[10px] text-[#888]">Snooze</span>
				</button>

				{#if showSnoozeMenu}
					<div
						class="absolute bottom-full right-0 mb-1 bg-[#050505] border border-[#333] py-1 min-w-[150px]"
						transition:fly={{ y: 10, duration: 150 }}
					>
						<button
							class="w-full text-left px-3 py-1.5 text-[10px] text-emerald-400 hover:bg-[#111] flex items-center gap-2"
							on:click|stopPropagation={() => handleSnooze(24 * 60 * 60 * 1000)}
						>
							<input type="checkbox" class="accent-emerald-500 pointer-events-none" checked />
							<span>Pause all alerts</span>
						</button>
						<div class="border-t border-[#222] my-1"></div>
						{#each snoozeOptions as option}
							<button
								class="w-full text-left px-3 py-1.5 text-[10px] text-[#888] hover:bg-[#111] hover:text-white transition-colors"
								on:click|stopPropagation={() => handleSnooze(option.ms)}
							>
								{option.label}
							</button>
						{/each}
					</div>
				{/if}
			</div>
		{/if}
	</div>
{/if}
