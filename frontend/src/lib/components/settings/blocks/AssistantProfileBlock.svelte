<script lang="ts">
	// "What the assistant knows about you": the operator profile (USER.md) fed to
	// the assistant and Brain at every chat. Saves on its own button, apart from
	// the Settings save bar. Moved here from the old /settings/profile page.
	import { onMount } from 'svelte';
	import { beforeNavigate } from '$app/navigation';
	import {
		getProfile,
		putProfile,
		type OperatorProfileResponse,
		type OperatorProfileStructured
	} from '$lib/api/profile';

	let profile: OperatorProfileResponse | null = null;
	let loading = true;
	let saving = false;
	let error: string | null = null;
	let saved: string | null = null;
	let showPreview = false;

	let name = '';
	let timezone = '';
	let startingCapital: number | null = null;
	let riskPerTrade: number | null = null;
	let exchange = '';
	let assetUniverse = '';
	let riskAppetite: 'conservative' | 'balanced' | 'aggressive' | '' = '';
	let responseStyle: 'terse' | 'conversational' | 'verbose' | '' = '';
	let quietHours = '';
	// Notification channels are managed in the dedicated Notifications settings
	// area; we round-trip whatever the backend already has so saving the profile
	// never clobbers channels configured elsewhere.
	let notificationChannels: string[] = [];
	let rules: string[] = [];
	let body = '';

	// Dirty-tracking: snapshot the loaded form so we can warn before navigating
	// away with unsaved edits (mirrors the parent /settings beforeNavigate guard).
	let pristine = '';
	let dirty = false;

	function snapshot(): string {
		return JSON.stringify({
			name,
			timezone,
			startingCapital,
			riskPerTrade,
			exchange,
			assetUniverse,
			riskAppetite,
			responseStyle,
			quietHours,
			notificationChannels,
			rules,
			body
		});
	}

	// The fields are listed so Svelte re-runs this when any of them changes.
	$: dirty =
		!loading &&
		(name, timezone, startingCapital, riskPerTrade, exchange, assetUniverse, riskAppetite, responseStyle, quietHours, rules, body, true) &&
		snapshot() !== pristine;

	function applyStructured(s: OperatorProfileStructured | null) {
		name = s?.name ?? '';
		timezone = s?.timezone ?? '';
		startingCapital = s?.starting_capital_usd ?? null;
		riskPerTrade = s?.risk_per_trade_pct ?? null;
		exchange = s?.exchange ?? '';
		assetUniverse = s?.asset_universe ?? '';
		riskAppetite = (s?.preferences?.risk_appetite as typeof riskAppetite) ?? '';
		responseStyle = (s?.preferences?.response_style as typeof responseStyle) ?? '';
		quietHours = s?.preferences?.quiet_hours ?? '';
		notificationChannels = [...(s?.preferences?.notification_channels ?? [])];
		rules = [...(s?.rules ?? [])];
	}

	function validate(): string | null {
		if (startingCapital !== null && (Number.isNaN(startingCapital) || startingCapital < 0))
			return 'Starting capital must be a non-negative number.';
		if (
			riskPerTrade !== null &&
			(Number.isNaN(riskPerTrade) || riskPerTrade < 0 || riskPerTrade > 100)
		)
			return 'Risk per trade must be between 0 and 100.';
		const qh = quietHours.trim();
		if (qh && !/^\d{1,2}:\d{2}-\d{1,2}:\d{2}$/.test(qh))
			return 'Quiet hours must look like "HH:MM-HH:MM" (e.g. 22:00-08:00).';
		return null;
	}

	async function load() {
		loading = true;
		error = null;
		try {
			const p = await getProfile();
			profile = p;
			applyStructured(p.structured);
			body = p.body ?? '';
			pristine = snapshot();
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		} finally {
			loading = false;
		}
	}

	async function save() {
		const validationError = validate();
		if (validationError) {
			error = validationError;
			saved = null;
			return;
		}
		saving = true;
		error = null;
		saved = null;
		try {
			const cleanRules = rules.map((r) => r.trim()).filter(Boolean);
			const payload = {
				structured: {
					name: name.trim() || null,
					timezone: timezone.trim() || null,
					starting_capital_usd: startingCapital,
					risk_per_trade_pct: riskPerTrade,
					exchange: exchange.trim() || null,
					asset_universe: assetUniverse.trim() || null,
					preferences: {
						notification_channels: notificationChannels,
						quiet_hours: quietHours.trim() || null,
						risk_appetite: riskAppetite || null,
						response_style: responseStyle || null
					},
					rules: cleanRules
				},
				body
			};
			const updated = await putProfile(payload);
			profile = updated;
			applyStructured(updated.structured);
			body = updated.body ?? '';
			pristine = snapshot();
			saved = 'Saved.';
			setTimeout(() => (saved = null), 3000);
		} catch (exc) {
			error = exc instanceof Error ? exc.message : String(exc);
		} finally {
			saving = false;
		}
	}

	function addRule() {
		rules = [...rules, ''];
	}

	function removeRule(i: number) {
		rules = rules.filter((_, idx) => idx !== i);
	}

	function previewSystemPrompt(): string {
		const lines: string[] = ['# OPERATOR PROFILE'];
		if (name) lines.push(`- Name: ${name}`);
		if (timezone) lines.push(`- Timezone: ${timezone}`);
		if (startingCapital !== null && startingCapital !== undefined)
			lines.push(`- Starting capital: $${startingCapital.toLocaleString()}`);
		if (riskPerTrade !== null && riskPerTrade !== undefined)
			lines.push(`- Risk per trade: ${riskPerTrade}%`);
		if (exchange) lines.push(`- Exchange: ${exchange}`);
		if (assetUniverse) lines.push(`- Asset universe: ${assetUniverse}`);
		if (riskAppetite) lines.push(`- Risk appetite: ${riskAppetite}`);
		if (responseStyle) lines.push(`- Response style: ${responseStyle}`);
		if (quietHours) lines.push(`- Quiet hours: ${quietHours}`);
		const cleanRules = rules.map((r) => r.trim()).filter(Boolean);
		if (cleanRules.length) {
			lines.push('- Rules:');
			cleanRules.forEach((r, i) => lines.push(`  ${i + 1}. ${r}`));
		}
		const trimmedBody = body.trim();
		if (trimmedBody) {
			lines.push('');
			lines.push(trimmedBody);
		}
		return lines.join('\n');
	}

	onMount(load);

	beforeNavigate((navigation) => {
		if (!dirty) return;
		const proceed = window.confirm(
			'You have unsaved changes to what the assistant knows about you. Leave and discard them?'
		);
		if (!proceed) navigation.cancel();
	});
</script>

<div class="grid gap-4 border-b border-sc-line py-3" data-testid="assistant-profile">
	{#if loading}
		<p class="m-0 text-xs text-sc-ink3">Loading…</p>
	{:else if error && !profile}
		<p class="m-0 text-xs text-red-300">{error}</p>
	{:else}
		<p class="m-0 text-xs text-sc-ink3">
			The assistant and Brain read this at every chat. Leave out anything that does not matter; empty fields are skipped.
		</p>
		{#if profile && !profile.exists}
			<p class="m-0 rounded border border-sc-line px-3 py-2 text-xs text-sc-ink2">
				Nothing saved yet. Your first save creates <code>USER.md</code> in the workspace.
			</p>
		{/if}
		{#if profile?.parse_error}
			<p class="m-0 rounded border border-amber-500/40 px-3 py-2 text-xs text-amber-300">
				<code>USER.md</code> could not be read ({profile.parse_error}). Saving here rewrites it from this form.
			</p>
		{/if}

		<section class="grid gap-2">
			<h4 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">About you</h4>
			<div class="grid gap-3 sm:grid-cols-2">
				<label class="grid gap-1 text-[12px]"><span class="text-[11px] text-sc-ink3">Name</span><input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" type="text" bind:value={name} placeholder="Your name" /></label>
				<label class="grid gap-1 text-[12px]"><span class="text-[11px] text-sc-ink3">Timezone</span><input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" type="text" bind:value={timezone} placeholder="America/Chicago" /></label>
			</div>
		</section>

		<section class="grid gap-2">
			<h4 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">How you trade</h4>
			<p class="m-0 text-xs text-sc-ink3">
				Background for the assistant only. The limits the engine enforces live under Live trading &amp; risk; keep the two consistent so the assistant is not told something different.
			</p>
			<div class="grid gap-3 sm:grid-cols-2">
				<label class="grid gap-1 text-[12px]"><span class="text-[11px] text-sc-ink3">Starting capital (USD)</span><input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" type="number" bind:value={startingCapital} step="100" /></label>
				<label class="grid gap-1 text-[12px]"><span class="text-[11px] text-sc-ink3">Risk per trade (%)</span><input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" type="number" bind:value={riskPerTrade} step="0.1" min="0" /></label>
				<label class="grid gap-1 text-[12px]"><span class="text-[11px] text-sc-ink3">Exchange</span><input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" type="text" bind:value={exchange} placeholder="hyperliquid" /></label>
				<label class="grid gap-1 text-[12px]"><span class="text-[11px] text-sc-ink3">Markets</span><input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" type="text" bind:value={assetUniverse} placeholder="crypto" /></label>
			</div>
		</section>

		<section class="grid gap-2">
			<h4 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Preferences</h4>
			<div class="grid gap-3 sm:grid-cols-3">
				<label class="grid gap-1 text-[12px]">
					<span class="text-[11px] text-sc-ink3">Risk appetite</span>
					<select class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" bind:value={riskAppetite}>
						<option value="">Not set</option>
						<option value="conservative">Conservative</option>
						<option value="balanced">Balanced</option>
						<option value="aggressive">Aggressive</option>
					</select>
				</label>
				<label class="grid gap-1 text-[12px]">
					<span class="text-[11px] text-sc-ink3">Reply style</span>
					<select class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" bind:value={responseStyle}>
						<option value="">Not set</option>
						<option value="terse">Short</option>
						<option value="conversational">Conversational</option>
						<option value="verbose">Detailed</option>
					</select>
				</label>
				<label class="grid gap-1 text-[12px]"><span class="text-[11px] text-sc-ink3">Quiet hours</span><input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none" type="text" bind:value={quietHours} placeholder="22:00-08:00" /></label>
			</div>
		</section>

		<section class="grid gap-2">
			<div class="flex items-center justify-between">
				<h4 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Rules the assistant must follow</h4>
				<button class="rounded border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink" type="button" on:click={addRule}>Add rule</button>
			</div>
			{#if rules.length === 0}
				<p class="m-0 text-xs text-sc-ink3">None yet. For example: "never suggest going live without a backtest".</p>
			{:else}
				<ul class="m-0 grid list-none gap-2 p-0">
					{#each rules as rule, i}
						<li class="flex gap-2">
							<input class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none flex-1" type="text" bind:value={rules[i]} aria-label="Rule {i + 1}" />
							<button class="text-[11px] text-sc-ink3 hover:text-sc-ink" type="button" on:click={() => removeRule(i)}>Remove</button>
						</li>
					{/each}
				</ul>
			{/if}
		</section>

		<section class="grid gap-2">
			<h4 class="m-0 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Anything else</h4>
			<textarea class="rounded border border-sc-line bg-sc-bg px-2 py-1.5 text-[13px] text-sc-ink focus:border-sc-ink3 focus:outline-none w-full resize-y" rows="6" bind:value={body} placeholder="Anything else the assistant should know"></textarea>
		</section>

		<div class="flex flex-wrap items-center gap-3">
			<button
				type="button"
				class="rounded border border-[#7fb2ff]/45 px-3 py-1 text-[12px] text-[#a9cbff] hover:border-[#7fb2ff] hover:bg-[#7fb2ff]/10 disabled:opacity-50"
				disabled={saving || !dirty}
				data-testid="assistant-profile-save"
				on:click={save}
			>{saving ? 'Saving…' : 'Save'}</button>
			<button class="text-[11px] text-sc-ink3 hover:text-sc-ink" type="button" on:click={() => (showPreview = !showPreview)}>
				{showPreview ? 'Hide' : 'Show'} what the assistant sees
			</button>
			{#if saved}<span class="text-xs text-emerald-300">{saved}</span>{/if}
			{#if error}<span class="text-xs text-red-300">{error}</span>{/if}
		</div>
		{#if showPreview}
			<pre class="m-0 max-h-96 overflow-auto whitespace-pre-wrap rounded border border-sc-line bg-sc-bg p-3 text-[12px] text-sc-ink2">{previewSystemPrompt()}</pre>
		{/if}
	{/if}
</div>
