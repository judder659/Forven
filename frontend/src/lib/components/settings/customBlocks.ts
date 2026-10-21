// Custom blocks: the parts of the Settings page that are not plain manifest
// fields (live status, mode switches, preset dials, panels with their own save).
// SettingsAreaView renders a subsection's block above its fields; a subsection
// with a block shows even when it has no fields of its own.
import type { Component } from 'svelte';
import AgentsLinkBlock from './blocks/AgentsLinkBlock.svelte';
import PipelineStanceBlock from './blocks/PipelineStanceBlock.svelte';
import RealMoneyStatus from './blocks/RealMoneyStatus.svelte';
import ResearchBlock from './blocks/ResearchBlock.svelte';
import SystemModeCard from './blocks/SystemModeCard.svelte';
import ThroughputDialBlock from './blocks/ThroughputDialBlock.svelte';
import WalletsBlock from './blocks/WalletsBlock.svelte';
import NotificationChannelsCard from './NotificationChannelsCard.svelte';
import UpdatePanel from './UpdatePanel.svelte';
import SettingsDangerZone from './sections/SettingsDangerZone.svelte';

export interface CustomBlock {
  // Svelte components of differing prop shapes share this registry.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  component: Component<any>;
  props?: (settings: Record<string, unknown>) => Record<string, unknown>;
}

const withSettings = (settings: Record<string, unknown>) => ({ settings });

export const CUSTOM_BLOCKS: Record<string, CustomBlock> = {
  'automation-mode': { component: SystemModeCard },
  'trading-real-money': { component: RealMoneyStatus, props: withSettings },
  'hl-wallets': { component: WalletsBlock, props: withSettings },
  'lab-pipeline-preset': { component: PipelineStanceBlock, props: withSettings },
  'lab-research': { component: ResearchBlock, props: withSettings },
  'ai-agents-link': { component: AgentsLinkBlock },
  'system-throughput': { component: ThroughputDialBlock, props: withSettings },
  'notif-channels': { component: NotificationChannelsCard, props: withSettings },
  'system-updates': { component: UpdatePanel },
  'danger-factory-reset': { component: SettingsDangerZone, props: withSettings },
};
