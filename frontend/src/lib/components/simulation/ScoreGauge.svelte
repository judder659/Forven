<script lang="ts">
	export let score: number;
	export let maxScore: number;
	export let size: number = 100;

	$: percentage = Math.round((score / maxScore) * 100);
	$: circumference = 2 * Math.PI * 40;
	$: strokeDashoffset = circumference - (percentage / 100) * circumference;

	$: color =
		percentage >= 85
			? '#34d399' // emerald-400
			: percentage >= 70
				? '#34d399' // emerald-400
				: percentage >= 55
					? '#facc15' // yellow-400
					: percentage >= 40
						? '#f87171' // red-400
						: '#f87171'; // red-400
</script>

<div class="relative" style="width: {size}px; height: {size}px;">
	<svg viewBox="0 0 100 100" class="transform -rotate-90">
		<!-- Background circle -->
		<circle
			cx="50"
			cy="50"
			r="40"
			fill="none"
			stroke="#222"
			stroke-width="8"
		/>
		<!-- Progress circle -->
		<circle
			cx="50"
			cy="50"
			r="40"
			fill="none"
			stroke={color}
			stroke-width="8"
			stroke-linecap="round"
			stroke-dasharray={circumference}
			stroke-dashoffset={strokeDashoffset}
			class="transition-all duration-500 ease-out"
		/>
	</svg>
	<div class="absolute inset-0 flex flex-col items-center justify-center">
		<span class="text-2xl font-bold text-white">{percentage}%</span>
		<span class="text-xs text-[#666]">{score}/{maxScore}</span>
	</div>
</div>
