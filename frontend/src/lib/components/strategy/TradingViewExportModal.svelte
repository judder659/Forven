<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { addToast } from '$lib/stores/processTracker';

	export let script: string;
	export let filename = '';
	export let warnings: string[] = [];
	/** Optional deep-link attached to copy toasts (e.g. the strategy detail route). */
	export let toastLink: string | undefined = undefined;

	const dispatch = createEventDispatcher<{ close: void }>();
	let copyStatus = '';

	function close(): void {
		dispatch('close');
	}

	async function copy(): Promise<void> {
		if (!script) return;
		try {
			if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
				await navigator.clipboard.writeText(script);
			} else {
				const textarea = document.createElement('textarea');
				textarea.value = script;
				textarea.setAttribute('readonly', 'true');
				textarea.style.position = 'fixed';
				textarea.style.left = '-9999px';
				document.body.appendChild(textarea);
				textarea.select();
				document.execCommand('copy');
				textarea.remove();
			}
			copyStatus = 'Copied';
			addToast('TradingView Pine copied', 'success', toastLink);
		} catch (err) {
			copyStatus = 'Copy failed';
			addToast(err instanceof Error ? err.message : 'Copy failed', 'error', toastLink);
		}
	}

	// Focus the Close button on open so keyboard users land inside the modal and Escape
	// works without first tabbing in.
	function autofocusClose(node: HTMLElement) {
		node.focus();
	}
</script>

<!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
<div
	class="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-4"
	data-testid="tradingview-export-dialog"
	role="presentation"
	on:click={(e) => { if (e.target === e.currentTarget) close(); }}
	on:keydown={(e) => { if (e.key === 'Escape') close(); }}
>
	<div
		class="flex max-h-[88vh] w-full max-w-5xl flex-col overflow-hidden border border-[#222] bg-[#050505]"
		role="dialog"
		aria-modal="true"
		aria-labelledby="tv-export-title"
	>
		<div class="flex flex-wrap items-center justify-between gap-3 border-b border-[#1a1a1a] px-4 py-3">
			<div>
				<div id="tv-export-title" class="text-[10px] font-bold uppercase tracking-widest text-[#888]">TradingView Pine Strategy</div>
				<div class="mt-1 font-mono text-xs text-[#666]">{filename}</div>
			</div>
			<div class="flex items-center gap-2">
				{#if copyStatus}
					<span class="text-[11px] uppercase tracking-wide text-emerald-400">{copyStatus}</span>
				{/if}
				<button
					type="button"
					data-testid="copy-tradingview-script"
					class="border border-emerald-900 bg-emerald-500/10 px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-emerald-400 transition-colors hover:bg-emerald-500/20"
					on:click={() => void copy()}
				>
					Copy
				</button>
				<button
					type="button"
					data-testid="close-tradingview-export"
					class="border border-[#333] bg-black px-3 py-1.5 text-[10px] font-bold uppercase tracking-widest text-[#888] transition-colors hover:border-[#555] hover:text-white"
					use:autofocusClose
					on:click={close}
				>
					Close
				</button>
			</div>
		</div>
		{#if warnings.length > 0}
			<div class="border-b border-yellow-900/50 bg-yellow-500/5 px-4 py-2 text-xs text-yellow-500">
				{warnings[0]}
			</div>
		{/if}
		<textarea
			data-testid="tradingview-export-script"
			class="min-h-[520px] flex-1 resize-none overflow-auto bg-black p-4 font-mono text-xs leading-relaxed text-[#ccc] outline-none"
			readonly
			spellcheck="false"
			value={script}
		></textarea>
	</div>
</div>
