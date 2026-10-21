<script lang="ts">
	/**
	 * Live probation's three automation switches. They save at once (not with the
	 * page's Save bar) because each one changes what Forven does with real money:
	 * turning entry or graduation on, or demotion off, needs the typed PROBATION.
	 */
	import { onMount } from 'svelte';
	import {
		PROBATION_CONFIRM_PHRASE,
		getProbationAutomation,
		probationRefusal,
		setProbationAutomation,
		type ProbationAutomation,
	} from '$lib/api/probation';
	import TypedConfirmDialog from '$lib/components/probation/TypedConfirmDialog.svelte';

	type Key = keyof ProbationAutomation;

	const SWITCHES: Array<{ key: Key; label: string; on: string; off: string; riskyWhen: boolean }> = [
		{
			key: 'auto_entry',
			label: 'Start probation automatically',
			on: 'Paper strategies that pass every check start live probation by themselves, and their size steps up the ladder as live trades build.',
			off: 'Forven lists ready strategies and notifies you; you start each one and raise its size by hand.',
			riskyWhen: true,
		},
		{
			key: 'auto_graduate',
			label: 'Graduate to full size automatically',
			on: 'A strategy that clears every graduation check moves to full size by itself.',
			off: 'Forven notifies you when a strategy is ready to graduate; you confirm it with GO LIVE.',
			riskyWhen: true,
		},
		{
			key: 'auto_demote',
			label: 'Demote automatically',
			on: 'A strategy whose live results fall outside what its backtest predicted goes back to paper by itself.',
			off: 'Forven only warns you; you demote by hand.',
			riskyWhen: false,
		},
	];

	let current: ProbationAutomation | null = null;
	let loadError: string | null = null;
	let pending: { key: Key; value: boolean } | null = null;
	let busy = false;
	let errorMessage: string | null = null;

	onMount(async () => {
		try {
			current = await getProbationAutomation();
		} catch (error) {
			loadError = error instanceof Error ? error.message : 'Could not read the probation switches';
		}
	});

	async function save(key: Key, value: boolean, confirm: string | null) {
		busy = true;
		errorMessage = null;
		try {
			current = await setProbationAutomation({ [key]: value, confirm });
			pending = null;
		} catch (error) {
			errorMessage = probationRefusal(error)?.message ?? (error instanceof Error ? error.message : 'Save failed');
		} finally {
			busy = false;
		}
	}

	function toggle(key: Key, riskyWhen: boolean) {
		if (!current || busy) return;
		const value = !current[key];
		if (value === riskyWhen) {
			errorMessage = null;
			pending = { key, value };
		} else {
			void save(key, value, null);
		}
	}

	$: pendingSwitch = pending ? SWITCHES.find((s) => s.key === pending?.key) : null;
</script>

<div class="grid gap-2 border-b border-sc-line py-3" data-testid="probation-automation">
	{#if loadError}
		<p class="text-xs text-red-300">{loadError}</p>
	{:else if current}
		{#each SWITCHES as item (item.key)}
			<div class="flex items-start justify-between gap-3">
				<div class="grid gap-0.5">
					<span class="text-sm text-sc-ink2">{item.label}</span>
					<span class="text-xs text-sc-ink3">{current[item.key] ? item.on : item.off}</span>
				</div>
				<button
					type="button"
					role="switch"
					aria-checked={current[item.key]}
					aria-label={item.label}
					class="mt-0.5 inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors {current[item.key] ? 'border-amber-500/70 bg-amber-500/40' : 'border-sc-line bg-sc-panel2'}"
					disabled={busy}
					data-testid={`probation-switch-${item.key}`}
					on:click={() => toggle(item.key, item.riskyWhen)}
				><span class="h-3.5 w-3.5 rounded-full bg-sc-ink transition-transform {current[item.key] ? 'translate-x-[18px]' : 'translate-x-[2px]'}"></span></button>
			</div>
		{/each}
		{#if errorMessage && !pending}<p class="text-xs text-red-300">{errorMessage}</p>{/if}
	{:else}
		<p class="text-xs text-sc-ink3">Loading…</p>
	{/if}
</div>

{#if pending && pendingSwitch}
	<TypedConfirmDialog
		title={`${pending.value ? 'Turn on' : 'Turn off'}: ${pendingSwitch.label.toLowerCase()}?`}
		phrase={PROBATION_CONFIRM_PHRASE}
		confirmLabel={pending.value ? 'Turn on' : 'Turn off'}
		message={pending.value ? pendingSwitch.on : pendingSwitch.off}
		{busy}
		{errorMessage}
		on:cancel={() => { pending = null; errorMessage = null; }}
		on:confirm={(event) => pending && save(pending.key, pending.value, event.detail.phrase)}
	/>
{/if}
