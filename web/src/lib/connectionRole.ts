import type { ComputedRef, InjectionKey } from 'vue'

/**
 * The node's connection role as App.vue's startup poll last read it. App.vue
 * owns the probe and the client-mode banner; this key only lets quiet chrome
 * (the Home header's status pill) restate the same fact without a second
 * poll. `unknown` means the probe failed or reported an invalid role, which is
 * never evidence of host mode.
 */
export type ConnectionRole =
  | { kind: 'host' }
  | { kind: 'client'; hostLabel: string; reachable: boolean }
  | { kind: 'unknown' }

export const CONNECTION_ROLE_KEY: InjectionKey<ComputedRef<ConnectionRole>> = Symbol('connection-role')
