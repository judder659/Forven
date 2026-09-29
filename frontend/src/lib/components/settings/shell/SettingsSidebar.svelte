<script lang="ts">
  import { SETTINGS_AREAS } from '$lib/settings/manifest';
  export let active: string;
  export let onChange: (id: string) => void = () => {};
  /** Areas suppressed from the nav (PORT-GATE-1: dark features hide their tab). */
  export let hiddenAreas: string[] = [];
  $: visibleAreas = SETTINGS_AREAS.filter((a) => !hiddenAreas.includes(a.id));
</script>

<nav class="flex flex-col gap-px p-2 border-r border-sc-line min-w-[14rem] sticky top-6 self-start max-h-[calc(100vh-3rem)] overflow-y-auto">
  {#each visibleAreas as area (area.id)}
    <button
      type="button"
      aria-current={active === area.id ? 'page' : undefined}
      on:click={() => onChange(area.id)}
      class="text-left text-[12px] px-3 py-2 border-l-2 transition-colors {
        active === area.id
          ? (area.danger ? 'border-red-500 bg-sc-panel2 text-red-400' : 'border-sc-ink bg-sc-panel2 text-sc-ink')
          : (area.danger ? 'border-transparent text-red-400/70 hover:text-red-300 hover:bg-sc-panel2' : 'border-transparent text-sc-ink2 hover:text-sc-ink hover:bg-sc-panel2')
      }"
    >
      {area.label}
    </button>
  {/each}
</nav>
