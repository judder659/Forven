import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

vi.mock('$lib/api/skills', () => ({
	getSkill: vi.fn(async () => ({ name: 'demo-skill', version: 1, description: 'Old' })),
}));

import SkillUpdateProposalCard from '../lib/components/approvals/SkillUpdateProposalCard.svelte';

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

beforeEach(() => {
	target = document.createElement('div');
	document.body.appendChild(target);
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target.remove();
});

describe('SkillUpdateProposalCard', () => {
	// Same keys propose_skill_update (forven/agents/tools_brain.py) writes.
	it('shows the bullets the Brain proposed instead of a "no diff" warning', () => {
		instance = mount(SkillUpdateProposalCard, {
			target,
			props: {
				payload: {
					skill_name: 'demo-skill',
					current_version: 1,
					proposed_description: 'New description',
					add_what_works: ['Works on TRX 4h'],
					add_what_doesnt_work: ['Fails in HIGH_VOL', 'Holdout too thin'],
					metadata_updates: null,
				},
			},
		});
		flushSync();

		const text = target.textContent ?? '';
		expect(text).not.toContain('Proposal has no diff');
		expect(text).toContain('New description');
		expect(text).toContain('Works on TRX 4h');
		expect(text).toContain('Fails in HIGH_VOL');
		expect(text).toContain('Holdout too thin');
	});

	it('warns when the payload proposes nothing', () => {
		instance = mount(SkillUpdateProposalCard, {
			target,
			props: { payload: { skill_name: 'demo-skill', add_what_works: [], add_what_doesnt_work: [] } },
		});
		flushSync();

		expect(target.textContent ?? '').toContain('Proposal has no diff');
	});
});
