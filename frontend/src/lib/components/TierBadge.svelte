<script lang="ts">
	export let tier: string = 'weak';

	function getConfig(t: string): { label: string; bg: string; text: string; border: string } {
		if (t === 'elite') return { label: 'E', bg: 'bg-emerald-500/10', text: 'text-emerald-400', border: 'border-emerald-900' };
		if (t === 'strong') return { label: 'S', bg: 'bg-transparent', text: 'text-white', border: 'border-[#555]' };
		if (t === 'marginal') return { label: 'M', bg: 'bg-yellow-500/10', text: 'text-yellow-400', border: 'border-yellow-900' };
		return { label: 'W', bg: 'bg-red-500/10', text: 'text-red-400', border: 'border-red-900' };
	}

	$: cfg = getConfig(tier);
</script>

<span
	class="inline-flex items-center justify-center w-5 h-5 text-[10px] font-bold uppercase border {cfg.bg} {cfg.text} {cfg.border}"
	title="{tier.charAt(0).toUpperCase() + tier.slice(1)} tier"
>{cfg.label}</span>
