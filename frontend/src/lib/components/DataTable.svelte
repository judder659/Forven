<script lang="ts">
	type Align = 'left' | 'right' | 'center';

	interface DataTableColumn {
		key: string;
		label: string;
		align?: Align;
		headerClass?: string;
		cellClass?: string;
	}

	export let columns: DataTableColumn[] = [];
	export let rows: unknown[] = [];
	export let rowKey: string | ((row: unknown, index: number) => string | number) = 'id';
	export let tableClass = 'w-full text-xs';
	export let headerClass = 'bg-[#111]';
	export let rowClass = 'border-t border-[#1a1a1a] hover:bg-[#111]';
	export let emptyText = 'No rows';
	export let emptyClass = 'px-4 py-8 text-center text-[#555]';
	export let stickyHeader = false;

	function alignClass(align: Align | undefined): string {
		if (align === 'right') return 'text-right';
		if (align === 'center') return 'text-center';
		return 'text-left';
	}

	function resolveRowKey(row: unknown, index: number): string | number {
		if (typeof rowKey === 'function') {
			return rowKey(row, index);
		}
		if (row && typeof row === 'object' && rowKey in row) {
			const value = (row as Record<string, unknown>)[rowKey];
			if (typeof value === 'string' || typeof value === 'number') {
				return value;
			}
		}
		return index;
	}

	function getCellValue(row: unknown, key: string): unknown {
		if (!row || typeof row !== 'object') return undefined;
		return (row as Record<string, unknown>)[key];
	}
</script>

<table class={tableClass}>
	<thead class="{headerClass} {stickyHeader ? 'sticky top-0 z-10' : ''}">
		<tr>
			{#each columns as column (column.key)}
				<th class="px-3 py-2 {alignClass(column.align)} {column.headerClass ?? ''}">
					{column.label}
				</th>
			{/each}
		</tr>
	</thead>
	<tbody>
		{#if rows.length > 0}
			{#each rows as row, index (resolveRowKey(row, index))}
				<tr class={rowClass}>
					{#each columns as column (column.key)}
						<td class="px-3 py-1.5 {alignClass(column.align)} {column.cellClass ?? ''}">
							<slot name="cell" {row} {column} {index}>
								{String(getCellValue(row, column.key) ?? '\u2014')}
							</slot>
						</td>
					{/each}
				</tr>
			{/each}
		{:else}
			<tr>
				<td colspan={Math.max(columns.length, 1)} class={emptyClass}>
					{emptyText}
				</td>
			</tr>
		{/if}
	</tbody>
</table>
