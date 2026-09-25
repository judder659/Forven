import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mount, tick, unmount } from 'svelte';

import ResearchSettingsPanel from '../lib/components/settings/ResearchSettingsPanel.svelte';
import type { ResearchSettings } from '$lib/api';

type MountedComponent = ReturnType<typeof mount>;

function buildSettings(): ResearchSettings {
	return {
		external_benchmarking_enabled: true,
		allowed_external_source_types: ['reddit', 'youtube', 'blog', 'github', 'forum', 'book', 'paper'],
	};
}

function buildSettingsWithCustomSource(): ResearchSettings {
	return {
		...buildSettings(),
		allowed_external_source_types: [...buildSettings().allowed_external_source_types, 'podcast'],
	};
}

async function flush(): Promise<void> {
	await Promise.resolve();
	await tick();
}

describe('ResearchSettingsPanel', () => {
	let target: HTMLDivElement;
	let app: MountedComponent | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
	});

	it('emits the edited research settings payload when saved', async () => {
		const saves: ResearchSettings[] = [];
		app = mount(ResearchSettingsPanel, {
			target,
			props: {
				draft: buildSettings(),
				onsave: (event: CustomEvent<ResearchSettings>) => saves.push(event.detail),
			},
		});
		await flush();

		const benchmarkingToggle = target.querySelector<HTMLInputElement>('[data-testid="research-external-benchmarking"]');
		expect(benchmarkingToggle).not.toBeNull();
		if (!benchmarkingToggle) return;
		benchmarkingToggle.checked = false;
		benchmarkingToggle.dispatchEvent(new Event('change', { bubbles: true }));

		const coverageInput = target.querySelector<HTMLInputElement>('[data-testid="research-min-feed-coverage-pct"]');
		expect(coverageInput).not.toBeNull();
		if (!coverageInput) return;
		coverageInput.value = '70';
		coverageInput.dispatchEvent(new Event('input', { bubbles: true }));

		const redditSourceToggle = target.querySelector<HTMLInputElement>('[data-testid="research-source-reddit"]');
		expect(redditSourceToggle).not.toBeNull();
		if (!redditSourceToggle) return;
		redditSourceToggle.checked = false;
		redditSourceToggle.dispatchEvent(new Event('change', { bubbles: true }));

		target.querySelector<HTMLButtonElement>('[data-testid="research-save"]')?.click();
		await flush();

		expect(saves).toHaveLength(1);
		expect(saves[0].external_benchmarking_enabled).toBe(false);
		expect(saves[0].candidate_min_feed_coverage_pct).toBe(70);
		expect(saves[0].allowed_external_source_types).not.toContain('reddit');
	});

	it('preserves unknown source types while editing known ones', async () => {
		const saves: ResearchSettings[] = [];
		app = mount(ResearchSettingsPanel, {
			target,
			props: {
				draft: buildSettingsWithCustomSource(),
				onsave: (event: CustomEvent<ResearchSettings>) => saves.push(event.detail),
			},
		});
		await flush();

		expect(target.textContent).toContain('podcast');

		const githubSourceToggle = target.querySelector<HTMLInputElement>('[data-testid="research-source-github"]');
		expect(githubSourceToggle).not.toBeNull();
		if (!githubSourceToggle) return;
		githubSourceToggle.checked = false;
		githubSourceToggle.dispatchEvent(new Event('change', { bubbles: true }));

		target.querySelector<HTMLButtonElement>('[data-testid="research-save"]')?.click();
		await flush();

		expect(saves).toHaveLength(1);
		expect(saves[0].allowed_external_source_types).toContain('podcast');
		expect(saves[0].allowed_external_source_types).not.toContain('github');
	});
});
