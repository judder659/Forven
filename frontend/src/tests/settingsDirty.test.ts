import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';

// Inject two fake manifest entries so groupDirtyByBackendSection has something to map.
vi.mock('$lib/settings/manifest', () => ({
  SETTINGS_MANIFEST: [
    {
      id: 'risk.max_daily_loss',
      label: 'Max daily loss',
      default: 200,
      type: 'number',
      area: 'trading',
      subsection: 'trading-risk-loss-limits',
      backendSection: 'risk',
      backendPath: 'max_daily_loss',
      description: '.',
      usedBy: ['x'],
    },
    {
      id: 'pipeline.quick_screen.min_sharpe',
      label: 'Quick-screen min Sharpe',
      default: 0.5,
      type: 'number',
      area: 'lab',
      subsection: 'lab-pipeline-quick-screen',
      backendSection: 'pipeline',
      backendPath: 'quick_screen.min_sharpe',
      description: '.',
      usedBy: ['x'],
    },
    {
      // Deep dotted path (3 levels) — a held-back-test knob writes
      // research_settings.research_holdout.* via section 'research';
      // grouping must build the full nested body.
      id: 'research.research_holdout.min_trades',
      label: 'Held-back min trades',
      default: 5,
      type: 'number',
      area: 'lab',
      subsection: 'lab-research',
      backendSection: 'research',
      backendPath: 'research_settings.research_holdout.min_trades',
      description: '.',
      usedBy: ['x'],
    },
  ],
}));

import {
  dirtyFields,
  originalValues,
  pendingValues,
  markField,
  revertField,
  clearDirty,
  groupDirtyByBackendSection,
  listEmptyNumericDirtyFields,
} from '$lib/settings/dirty';

describe('dirty store', () => {
  beforeEach(() => {
    clearDirty();
    originalValues.set({});
  });

  it('tracks a field as dirty when value differs from original', () => {
    originalValues.set({ 'risk.max_daily_loss': 200 });
    markField('risk.max_daily_loss', 150);
    expect(get(dirtyFields).has('risk.max_daily_loss')).toBe(true);
  });

  it('removes field from dirty when set back to original', () => {
    originalValues.set({ 'risk.max_daily_loss': 200 });
    markField('risk.max_daily_loss', 150);
    markField('risk.max_daily_loss', 200);
    expect(get(dirtyFields).has('risk.max_daily_loss')).toBe(false);
  });

  it('treats structurally-equal objects as not dirty', () => {
    originalValues.set({ 'obj.setting': { a: 1, b: [2, 3] } });
    markField('obj.setting', { a: 1, b: [2, 3] });
    expect(get(dirtyFields).has('obj.setting')).toBe(false);
  });

  it('revertField clears dirty', () => {
    originalValues.set({ 'risk.max_daily_loss': 200 });
    markField('risk.max_daily_loss', 150);
    revertField('risk.max_daily_loss');
    expect(get(dirtyFields).has('risk.max_daily_loss')).toBe(false);
  });

  it('groups dirty fields by backend section using manifest', () => {
    originalValues.set({
      'risk.max_daily_loss': 200,
      'pipeline.quick_screen.min_sharpe': 0.5,
    });
    markField('risk.max_daily_loss', 150);
    markField('pipeline.quick_screen.min_sharpe', 0.8);
    const grouped = groupDirtyByBackendSection({
      'risk.max_daily_loss': 150,
      'pipeline.quick_screen.min_sharpe': 0.8,
    });
    expect(Object.keys(grouped).sort()).toEqual(['pipeline', 'risk']);
    expect(grouped.risk).toEqual({ max_daily_loss: 150 });
    expect(grouped.pipeline).toEqual({ quick_screen: { min_sharpe: 0.8 } });
  });

  it('groups a deep dotted research path into the full nested body', () => {
    originalValues.set({ 'research.research_holdout.min_trades': 5 });
    markField('research.research_holdout.min_trades', 12);
    const grouped = groupDirtyByBackendSection({
      'research.research_holdout.min_trades': 12,
    });
    expect(grouped).toEqual({
      research: {
        research_settings: { research_holdout: { min_trades: 12 } },
      },
    });
  });

  it('groupDirtyByBackendSection returns empty object when nothing dirty', () => {
    expect(groupDirtyByBackendSection({})).toEqual({});
  });

  it('ignores dirty field ids not present in the manifest', () => {
    // Somehow a stale/removed id ends up in the dirty set — must not crash.
    dirtyFields.set(new Set(['unknown.ghost_field']));
    const grouped = groupDirtyByBackendSection({ 'unknown.ghost_field': 42 });
    expect(grouped).toEqual({});
  });

  it('markField writes the value to pendingValues', () => {
    originalValues.set({ 'risk.max_daily_loss': 200 });
    markField('risk.max_daily_loss', 150);
    expect(get(pendingValues)['risk.max_daily_loss']).toBe(150);
  });

  it('revertField removes the id from pendingValues', () => {
    originalValues.set({ 'risk.max_daily_loss': 200 });
    markField('risk.max_daily_loss', 150);
    expect(get(pendingValues)['risk.max_daily_loss']).toBe(150);
    revertField('risk.max_daily_loss');
    expect('risk.max_daily_loss' in get(pendingValues)).toBe(false);
  });

  it('listEmptyNumericDirtyFields flags dirty number fields holding null/NaN', () => {
    originalValues.set({
      'risk.max_daily_loss': 200,
      'pipeline.quick_screen.min_sharpe': 0.5,
    });
    markField('risk.max_daily_loss', null);
    markField('pipeline.quick_screen.min_sharpe', 0.8);
    const empty = listEmptyNumericDirtyFields({
      'risk.max_daily_loss': null,
      'pipeline.quick_screen.min_sharpe': 0.8,
    });
    expect(empty).toEqual([{ id: 'risk.max_daily_loss', label: 'Max daily loss' }]);
  });

  it('listEmptyNumericDirtyFields returns empty when all numeric values are real numbers', () => {
    originalValues.set({ 'risk.max_daily_loss': 200 });
    markField('risk.max_daily_loss', 150);
    expect(listEmptyNumericDirtyFields({ 'risk.max_daily_loss': 150 })).toEqual([]);
  });

  it('listEmptyNumericDirtyFields ignores non-number manifest types and unknown ids', () => {
    dirtyFields.set(new Set(['unknown.ghost_field']));
    expect(listEmptyNumericDirtyFields({ 'unknown.ghost_field': null })).toEqual([]);
  });

  it('clearDirty clears pendingValues', () => {
    originalValues.set({ 'risk.max_daily_loss': 200 });
    markField('risk.max_daily_loss', 150);
    markField('pipeline.quick_screen.min_sharpe', 0.8);
    expect(Object.keys(get(pendingValues)).length).toBeGreaterThan(0);
    clearDirty();
    expect(get(pendingValues)).toEqual({});
  });
});
