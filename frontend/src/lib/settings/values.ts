// Reading manifest values out of the settings blob, shared by every area.
import { get } from 'svelte/store';
import { SETTINGS_MANIFEST, type SettingsEntry } from './manifest';
import { originalValues } from './dirty';

export function readByPath(obj: unknown, path: string): unknown {
  return path
    .split('.')
    .reduce<any>((cursor, key) => (cursor == null ? undefined : cursor[key]), obj);
}

/** The saved value of an entry (the blob is FLAT: backendSection only routes the PUT). */
export function entryValue(settings: unknown, entry: SettingsEntry): unknown {
  const v = readByPath(settings, entry.backendPath);
  return v === undefined ? entry.default : v;
}

/**
 * Seed the save bar's baseline for every manifest entry, so a block in one
 * area (a preset dial) can edit fields that live in another. Values already
 * seeded win: after a save the baseline holds the saved value, while the
 * settings blob the page loaded is still the old one.
 */
export function seedOriginals(settings: unknown): void {
  const seed: Record<string, unknown> = {};
  for (const entry of SETTINGS_MANIFEST) seed[entry.id] = entryValue(settings, entry);
  originalValues.update((o) => ({ ...seed, ...o }));
}

/** Current value of an entry: pending edit, else the seeded baseline, else the blob. */
export function currentValue(entry: SettingsEntry, settings: unknown, values: Record<string, unknown>): unknown {
  if (entry.id in values) return values[entry.id];
  const seeded = get(originalValues);
  return entry.id in seeded ? seeded[entry.id] : entryValue(settings, entry);
}

export function isEntryVisible(entry: SettingsEntry, settings: unknown, values: Record<string, unknown>): boolean {
  if (!entry.showWhen) return true;
  const other = SETTINGS_MANIFEST.find((e) => e.id === entry.showWhen!.id);
  if (!other) return true;
  const value = currentValue(other, settings, values);
  return entry.showWhen.equals.some((candidate) => same(candidate, value));
}

function same(a: unknown, b: unknown): boolean {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-9;
  if (a !== null && b !== null && typeof a === 'object' && typeof b === 'object') {
    return JSON.stringify(a) === JSON.stringify(b);
  }
  return a === b;
}

/**
 * Whether a field holds something other than its default. Secrets and blobs
 * owned by a custom block never count: their value is not shown as a field.
 */
export function differsFromDefault(entry: SettingsEntry, value: unknown): boolean {
  if (entry.type === 'secret' || entry.hidden) return false;
  if (value === undefined || value === null || value === '') {
    return !(entry.default === undefined || entry.default === null || entry.default === '');
  }
  if (entry.type === 'csv') {
    const list = (v: unknown) =>
      (Array.isArray(v) ? v : String(v ?? '').split(','))
        .map((item) => String(item).trim())
        .filter(Boolean)
        .join(',');
    return list(value) !== list(entry.default);
  }
  return !same(value, entry.default);
}
