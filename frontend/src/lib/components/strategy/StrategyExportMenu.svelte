<script lang="ts">
	import { exportStrategyContainer } from '$lib/api';
	import { addToast } from '$lib/stores/processTracker';
	import {
		buildExportFilename,
		copyJsonToClipboard,
		downloadJson,
	} from '$lib/utils/strategyPortability';

	/** Strategy container id (or display id) to export. */
	export let strategyId: string;
	/** Human-facing id used in the filename + toast (defaults to strategyId). */
	export let displayId: string = '';
	/** Strategy name, used to make the download filename friendlier. */
	export let name: string = '';
	/** Compact rendering for table rows / dense toolbars. */
	export let compact = false;

	let open = false;
	let busy = false;

	$: label = (displayId || strategyId || 'strategy').trim();

	function toggle(): void {
		if (busy) return;
		open = !open;
	}

	function close(): void {
		open = false;
	}

	async function fetchEnvelope(): Promise<Record<string, unknown> | null> {
		try {
			busy = true;
			return await exportStrategyContainer(strategyId);
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Export failed', 'error');
			return null;
		} finally {
			busy = false;
		}
	}

	async function exportDownload(): Promise<void> {
		close();
		const envelope = await fetchEnvelope();
		if (!envelope) return;
		downloadJson(envelope, buildExportFilename(label, name));
		addToast(`Exported ${label} to file`, 'success');
	}

	async function exportClipboard(): Promise<void> {
		close();
		const envelope = await fetchEnvelope();
		if (!envelope) return;
		try {
			await copyJsonToClipboard(envelope);
			addToast(`Copied ${label} export to clipboard`, 'success');
		} catch (err) {
			addToast(err instanceof Error ? err.message : 'Copy failed', 'error');
		}
	}
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div
	class="relative inline-block text-left"
	on:keydown={(e) => {
		if (e.key === 'Escape') close();
	}}
>
	<button
		type="button"
		data-testid="strategy-export-trigger"
		class={`inline-flex items-center gap-1 border border-[#333] bg-black font-bold uppercase tracking-widest text-[#888] transition-colors hover:border-[#555] hover:text-white disabled:opacity-50 ${
			compact ? 'px-2 py-1 text-[9px]' : 'px-3 py-1.5 text-[10px]'
		}`}
		disabled={busy}
		aria-haspopup="menu"
		aria-expanded={open}
		on:click|stopPropagation={toggle}
	>
		{busy ? '…' : '⤓'} Export
	</button>

	{#if open}
		<!-- Click-catcher closes the menu when clicking elsewhere. -->
		<button
			type="button"
			class="fixed inset-0 z-40 cursor-default"
			tabindex="-1"
			aria-label="Close export menu"
			on:click|stopPropagation={close}
		></button>
		<div
			class="absolute right-0 z-50 mt-1 w-44 overflow-hidden border border-[#222] bg-[#050505]"
			role="menu"
		>
			<button
				type="button"
				data-testid="strategy-export-download"
				class="block w-full px-3 py-2 text-left text-[11px] text-[#888] transition-colors hover:bg-[#111] hover:text-white"
				role="menuitem"
				on:click|stopPropagation={() => void exportDownload()}
			>
				⤓ Download .json
			</button>
			<button
				type="button"
				data-testid="strategy-export-clipboard"
				class="block w-full border-t border-[#1a1a1a] px-3 py-2 text-left text-[11px] text-[#888] transition-colors hover:bg-[#111] hover:text-white"
				role="menuitem"
				on:click|stopPropagation={() => void exportClipboard()}
			>
				⧉ Copy to clipboard
			</button>
		</div>
	{/if}
</div>
