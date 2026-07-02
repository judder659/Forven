<script lang="ts">
  export let label: string;
  export let description: string;
  export let deepLinkTo: string | undefined = undefined;
  export let usedBy: string[] = [];
  let usedByOpen = false;
</script>

<section class="terminal-card">
  <header class="flex flex-wrap items-center justify-between gap-2 border-b border-[#1a1a1a] px-4 py-2">
    <h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">{label}</h2>
    {#if deepLinkTo}
      <a href={deepLinkTo} class="text-[10px] uppercase tracking-wider text-[#666] hover:text-white">→ {deepLinkTo}</a>
    {/if}
  </header>
  <div class="space-y-4 p-4">
    <p class="text-xs text-[#888]">{description}</p>
    {#if usedBy.length}
      <button
        type="button"
        aria-expanded={usedByOpen}
        aria-controls={`used-by-${label}`}
        on:click={() => (usedByOpen = !usedByOpen)}
        class="text-[10px] uppercase tracking-wider text-[#666] hover:text-white"
      >
        Used by <span aria-hidden="true">{usedByOpen ? '▾' : '▸'}</span>
      </button>
      {#if usedByOpen}
        <ul id={`used-by-${label}`} class="text-xs text-[#888] pl-4 space-y-1">
          {#each usedBy as reader}<li>{reader}</li>{/each}
        </ul>
      {/if}
    {/if}
    <div class="space-y-1"><slot /></div>
  </div>
</section>
