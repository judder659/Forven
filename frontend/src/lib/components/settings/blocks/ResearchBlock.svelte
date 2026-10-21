<script lang="ts">
	// Research sources and the held-back data test. The panel saves on its own
	// (its Save button), not through the settings save bar.
	import ResearchSettingsPanel from '$lib/components/settings/ResearchSettingsPanel.svelte';
	import { updateSettingsSection, type ResearchSettings } from '$lib/api';

	export let settings: Record<string, unknown>;

	// Local draft for the ResearchSettingsPanel. The panel handles its own save flow
	// (emits 'save' / onsave) and does NOT participate in the pendingValues dirty graph.
	function defaultResearchSettings(): any {
		return {
			external_benchmarking_enabled: true,
			allowed_external_source_types: [],
			research_sources: {
				reddit: {
					enabled: true,
					subs: ['algotrading', 'quant', 'options', 'thetagang', 'systematictrading'],
					client_id: null,
					client_secret: null,
					rate_limit_per_min: 30,
				},
			},
		};
	}

	function mergeResearchDraft(raw: unknown): any {
		const base = defaultResearchSettings();
		if (!raw || typeof raw !== 'object') return base;
		const src = raw as Record<string, unknown>;
		return {
			...base,
			...src,
			research_sources: {
				...base.research_sources,
				...(src.research_sources as object ?? {}),
				reddit: {
					...base.research_sources.reddit,
					...(((src.research_sources as Record<string, unknown> | undefined)?.reddit as object) ?? {}),
				},
			},
			allowed_external_source_types: Array.isArray(src.allowed_external_source_types)
				? src.allowed_external_source_types
				: base.allowed_external_source_types,
		};
	}

	let researchSettingsDraft: any = mergeResearchDraft(settings?.research_settings);

	let researchSaving = false;
	let researchBanner: { tone: 'success' | 'error'; message: string } | null = null;

	async function handleResearchSave(event: CustomEvent<ResearchSettings>): Promise<void> {
		researchSaving = true;
		researchBanner = null;
		try {
			await updateSettingsSection('research', { research_settings: event.detail });
			researchSettingsDraft = mergeResearchDraft(event.detail);
			researchBanner = { tone: 'success', message: 'Research settings saved.' };
			setTimeout(() => (researchBanner = null), 2500);
		} catch (err) {
			researchBanner = {
				tone: 'error',
				message: err instanceof Error ? err.message : 'Failed to save research settings.',
			};
		} finally {
			researchSaving = false;
		}
	}

</script>

{#if researchBanner}
	<div
		class={`mb-3 border px-3 py-2 text-xs ${
			researchBanner.tone === 'success'
				? 'border-emerald-900 bg-emerald-500/5 text-emerald-400'
				: 'border-red-900 bg-red-500/5 text-red-400'
		}`}
	>
		{researchBanner.message}
	</div>
{/if}
<ResearchSettingsPanel
	bind:draft={researchSettingsDraft}
	saving={researchSaving}
	on:save={handleResearchSave}
/>
