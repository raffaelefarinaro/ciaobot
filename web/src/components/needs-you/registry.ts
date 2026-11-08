/** Per-kind needs-you UI: the body that owns the primary control, and the card icon. A kind with neither has no card. */
import type { Component } from 'vue'
import type { AppIconName } from '../AppIcon.vue'
import type { ChatActionKind } from '../../lib/types'
import McpAuthBody from './McpAuthBody.vue'
import RestartBody from './RestartBody.vue'
import CommandBody from './CommandBody.vue'
import PermissionModeBody from './PermissionModeBody.vue'
import SystemSettingBody from './SystemSettingBody.vue'
import DeviceCodeBody from './DeviceCodeBody.vue'
import GwsAuthBody from './GwsAuthBody.vue'

/** One body per kind; each later child registers its own here. */
export const NEEDS_YOU_BODIES: Partial<Record<ChatActionKind, Component>> = {
  mcp_auth: McpAuthBody,
  command: CommandBody,
  permission_mode: PermissionModeBody,
  system_setting: SystemSettingBody,
  device_code: DeviceCodeBody,
  restart: RestartBody,
  gws_auth: GwsAuthBody,
}

/** `command` has no entry on purpose: it shows the default 'activity' icon. */
export const NEEDS_YOU_ICONS: Partial<Record<ChatActionKind, AppIconName>> = {
  mcp_auth: 'shield',
  permission_mode: 'shield',
  system_setting: 'shield',
  device_code: 'shield',
  gws_auth: 'shield',
  restart: 'spark',
}
