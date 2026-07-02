<script lang="ts">
	/**
	 * Quiet OpenCode GO referral note. Same idea as the Hyperliquid referral CTA
	 * in SettingsTrading.svelte, but deliberately toned down — a single muted
	 * line, no bordered card — since it's a "nice to have" rather than a required
	 * setup step. The link carries our referral code; we label it as a referral
	 * so it's never disguised as a plain sign-up link.
	 */
	import { openExternal } from '$lib/external-open';

	const OPENCODE_GO_REFERRAL_URL = 'https://opencode.ai/go?ref=WCEAQYAK3R';
	let copyFallback = false;

	async function openReferral(): Promise<void> {
		// openExternal hands the URL to the system browser (Tauri opener) and
		// returns false if that fails; reveal a copy-able fallback in that case.
		copyFallback = !(await openExternal(OPENCODE_GO_REFERRAL_URL));
	}
</script>

<p class="text-xs text-[#666]">
	<a
		href={OPENCODE_GO_REFERRAL_URL}
		on:click|preventDefault={openReferral}
		class="text-white hover:text-[#888] hover:underline">Get OpenCode GO ↗</a>
	<span class="text-[#555]">— referral link: you get $5 of usage credit</span>
</p>
{#if copyFallback}
	<p class="mt-1 text-[11px] text-yellow-400 break-all">
		Couldn't open your browser. Copy this link: {OPENCODE_GO_REFERRAL_URL}
	</p>
{/if}
