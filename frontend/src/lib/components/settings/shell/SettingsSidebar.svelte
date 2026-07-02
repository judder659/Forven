<script lang="ts">
  import { SETTINGS_AREAS } from '$lib/settings/manifest';
  export let active: string;
  export let onChange: (id: string) => void = () => {};
</script>

<nav class="flex flex-col gap-px p-2 border-r border-[#222] min-w-[14rem] sticky top-6 self-start max-h-[calc(100vh-3rem)] overflow-y-auto">
  {#each SETTINGS_AREAS as area (area.id)}
    <button
      type="button"
      aria-current={active === area.id ? 'page' : undefined}
      on:click={() => onChange(area.id)}
      class="text-left text-xs uppercase tracking-wider px-3 py-2 border-l-2 transition-colors {
        active === area.id
          ? (area.danger ? 'border-red-500 bg-[#111] text-red-400' : 'border-white bg-[#111] text-white')
          : (area.danger ? 'border-transparent text-red-400/70 hover:text-red-300 hover:bg-[#111]' : 'border-transparent text-[#888] hover:text-white hover:bg-[#111]')
      }"
    >
      {area.label}
    </button>
  {/each}
</nav>
