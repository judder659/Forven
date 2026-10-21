<script lang="ts">
  export let label: string;
  export let description: string;
  export let deepLinkTo: string | undefined = undefined;
  /** Anchor id, so a deep link can land on the group. */
  export let id: string | undefined = undefined;
  /** Rarely-changed groups start folded. */
  export let collapsed = false;
  /** Fields in this group that differ from their defaults. */
  export let changedCount = 0;
  export let onReset: (() => void) | undefined = undefined;

  let open = !collapsed;
  let confirmingReset = false;

  function reset(): void {
    if (!confirmingReset) {
      confirmingReset = true;
      return;
    }
    confirmingReset = false;
    onReset?.();
  }
</script>

<section class="terminal-card" id={id ? `group-${id}` : undefined} data-testid={id ? `group-${id}` : undefined}>
  <header class="flex flex-wrap items-center justify-between gap-2 border-b border-sc-line px-4 py-2">
    <h2 class="flex items-center gap-2 text-[14px] font-semibold text-sc-ink2">
      {#if collapsed}
        <button
          type="button"
          class="flex items-center gap-2 hover:text-sc-ink"
          aria-expanded={open}
          on:click={() => (open = !open)}
        >
          <span aria-hidden="true">{open ? '▾' : '▸'}</span>{label}
          <span class="font-plex-cond text-[10px] font-medium uppercase tracking-[0.08em] text-sc-ink3">Advanced</span>
        </button>
      {:else}
        {label}
      {/if}
      {#if changedCount > 0}
        <span
          class="rounded-full bg-sc-panel2 px-1.5 font-plex-mono text-[10px] font-medium text-sc-ink3"
          title="{changedCount} field{changedCount === 1 ? '' : 's'} differ from the default"
          data-testid="changed-count"
        >{changedCount} changed</span>
      {/if}
    </h2>
    <div class="flex items-center gap-3">
      {#if onReset && open}
        <button
          type="button"
          on:click={reset}
          on:blur={() => (confirmingReset = false)}
          class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] {confirmingReset ? 'text-[#e7b24a]' : 'text-sc-ink3 hover:text-sc-ink'}"
          data-testid="reset-group"
        >
          {confirmingReset ? 'Click again to reset' : 'Reset group to defaults'}
        </button>
      {/if}
      {#if deepLinkTo}
        <a href={deepLinkTo} class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3 hover:text-sc-ink">→ {deepLinkTo}</a>
      {/if}
    </div>
  </header>
  <div class="space-y-4 p-4" hidden={!open}>
    <p class="text-xs text-sc-ink2">{description}</p>
    <div class="space-y-1"><slot /></div>
  </div>
</section>
