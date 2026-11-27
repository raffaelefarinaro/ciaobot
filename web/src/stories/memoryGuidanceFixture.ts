import { defineComponent, h } from 'vue'
import type { Component } from 'vue'
import type { RouteRecordRaw } from 'vue-router'
import InAppToast from '../components/InAppToast.vue'
import { useProjectStore } from '../stores/projects'
import type { FixtureRoute } from './fixtureBoundary'
import { mountConnected, type ConnectedFixture } from './mountConnected'

/**
 * Synthetic fixtures for the Memory guidance workshop stories (slice B16).
 * Names and payloads are `storybook-*` only: no real workspace, guide text,
 * token or user note. Every read is an exact declared route; everything else
 * the owners try fails closed at the preview boundary.
 */

export const guideWorkspace = 'storybook-alpha'
export const guideSecondWorkspace = 'storybook-beta'

/** A declare-able response that never resolves: the request is held until teardown. */
const held = (): Promise<never> => new Promise(() => {})

// ── Synthetic guide file ─────────────────────────────────────────────────────

/**
 * One bounded region's fixture: its entries and (optionally) a marker cap.
 * Serialization must mirror the owner's own math (`serializeLen` in
 * MemoryGuideBudget.vue: the entries joined with `\n§\n`, plus one trailing
 * newline), so the workshop expectations stay exact without duplicating the
 * rendering logic. If the owner's math changes, these exact plays fail —
 * which is the point.
 */
export type RegionSpec = { entries: string[]; cap?: number }

/** The guide body the component's region regexes read. */
export function guideBody(spec: {
  memory: RegionSpec
  profile: RegionSpec
}): string {
  const region = (start: string, key: 'Agent memory' | 'User profile', regionSpec: RegionSpec): string => {
    const cap = regionSpec.cap === undefined ? '' : ` cap=${regionSpec.cap}`
    const body = [`## ${key}`, regionSpec.entries.join('\n\n§\n\n')].join('\n')
    return [`<!-- ciao:${start}:start${cap} -->`, body, `<!-- ciao:${start}:end -->`].join('\n')
  }
  return [
    '# Storybook guide',
    '',
    '',
    region('memory', 'Agent memory', spec.memory),
    '',
    region('profile', 'User profile', spec.profile),
    '',
    '',
    'Synthetic filler that only pads the whole file the guide token line counts.',
  ].join('\n')
}

/**
 * The guide file the default stories load: small entries in both regions, no
 * expiry or tags, and nothing over its cap.
 */
export function defaultRegionSpecs(): { memory: RegionSpec; profile: RegionSpec } {
  return {
    memory: { entries: ['Remember the storybook handoff for the workshop.'] },
    profile: { entries: ['The workshop works from the storybook profile.'] },
  }
}

export function defaultGuideBody(): string {
  return guideBody(defaultRegionSpecs())
}

/** One region over its advisory cap, the other comfortably under it. */
export function overCapRegionSpecs(): { memory: RegionSpec; profile: RegionSpec } {
  return {
    memory: { entries: ['x'.repeat(120)], cap: 100 },
    profile: { entries: ['Storybook profile entry.'] },
  }
}

/** One region at the warn threshold (80% or more) without crossing its cap. */
export function highUsageRegionSpecs(): { memory: RegionSpec; profile: RegionSpec } {
  return {
    memory: { entries: ['x'.repeat(84)], cap: 100 },
    profile: { entries: ['Storybook profile entry.'] },
  }
}

/** One expired entry and one malformed tag, one per region line. */
export function flaggedRegionSpecs(): { memory: RegionSpec; profile: RegionSpec } {
  return {
    memory: { entries: ['An old workshop note [expires: 2026-01-01].'] },
    profile: { entries: ['A malformed workshop note [expires: 2026-02-30].'] },
  }
}

/** The owner-side serialization the region counts mirror. */
const serializeLen = (entries: string[]): number => entries.length ? entries.join('\n§\n').length + 1 : 0

/** One region's expected render strings, from the same math the owner uses. */
export function regionExpectation(spec: RegionSpec, fallbackCap: number): {
  used: number; cap: number; pct: number; count: string; label: string
} {
  const cap = spec.cap ?? fallbackCap
  const used = serializeLen(spec.entries)
  const pct = cap ? Math.round((used / cap) * 100 * 10) / 10 : 0
  return {
    used, cap, pct,
    count: `${used.toLocaleString()} / ${cap.toLocaleString()}`,
    label: `${pct}% of its cap`,
  }
}

// ── Declared routes ──────────────────────────────────────────────────────────

const plainText = (body: string, status = 200): Response =>
  new Response(body, { status, headers: { 'Content-Type': 'text/plain' } })
const notFound = (): Response =>
  new Response('Not found', { status: 404, headers: { 'Content-Type': 'text/plain' } })
const serverError = (message: string): Response =>
  new Response(JSON.stringify({ error: message }), { status: 503, headers: { 'Content-Type': 'application/json' } })

const qualifiedGuidePath = (workspace: string, file: string): string =>
  `/api/workspace-file?exact=1&path=${encodeURIComponent(`${workspace}/${file}`)}`
const bareGuidePath = (file: string): string =>
  `/api/workspace-file?exact=1&path=${encodeURIComponent(file)}`

export type GuideStoryState =
  | 'default' | 'loading' | 'over-cap' | 'high-usage'
  | 'expired-and-malformed' | 'legacy-claude-guide'
  | 'no-guide' | 'error' | 'refresh'

/**
 * The declared reads for one MemoryGuideBudget story. Only the guide probe the
 * owner really makes is answered (the qualified error case deliberately
 * leaves the bare names undeclared: the owner must stop before them).
 */
export function guideRoutes(state: GuideStoryState): FixtureRoute[] {
  const alphaAgents = qualifiedGuidePath(guideWorkspace, 'AGENTS.md')
  const alphaClaude = qualifiedGuidePath(guideWorkspace, 'CLAUDE.md')
  const content =
    state === 'over-cap'
      ? guideBody(overCapRegionSpecs())
      : state === 'high-usage'
        ? guideBody(highUsageRegionSpecs())
        : state === 'expired-and-malformed'
          ? guideBody(flaggedRegionSpecs())
          : defaultGuideBody()
  switch (state) {
    case 'default':
    case 'over-cap':
    case 'high-usage':
    case 'expired-and-malformed':
      return [{ method: 'GET', path: alphaAgents, respond: () => plainText(content) }]
    case 'loading':
      return [{ method: 'GET', path: alphaAgents, respond: held }]
    case 'legacy-claude-guide':
      return [
        { method: 'GET', path: alphaAgents, respond: notFound },
        { method: 'GET', path: alphaClaude, respond: () => plainText(content) },
      ]
    case 'no-guide':
      return [
        { method: 'GET', path: alphaAgents, respond: notFound },
        { method: 'GET', path: alphaClaude, respond: notFound },
        { method: 'GET', path: bareGuidePath('AGENTS.md'), respond: notFound },
        { method: 'GET', path: bareGuidePath('CLAUDE.md'), respond: notFound },
      ]
    case 'error':
      return [
        { method: 'GET', path: alphaAgents, respond: () => serverError('Synthetic guide read refused') },
        { method: 'GET', path: alphaClaude, respond: () => serverError('Synthetic guide read refused') },
      ]
    case 'refresh':
      return [
        { method: 'GET', path: alphaAgents, respond: () => plainText(content) },
        { method: 'GET', path: qualifiedGuidePath(guideSecondWorkspace, 'AGENTS.md'), respond: held },
      ]
  }
}

/**
 * The reads the Open action makes through the file viewer store: the vault's
 * markdown path index and the file itself (a plain `/api/workspace-file`
 * lookup, without the guide probe's `exact=1`).
 */
export function viewerRoutes(guidePath: string): FixtureRoute[] {
  return [
    { method: 'GET', path: '/api/vault-markdown-paths', respond: () => new Response(JSON.stringify({ paths: [] }), { status: 200, headers: { 'Content-Type': 'application/json' } }) },
    { method: 'GET', path: `/api/workspace-file?path=${encodeURIComponent(guidePath)}`, respond: () => plainText(defaultGuideBody()) },
  ]
}

// ── Router ───────────────────────────────────────────────────────────────────

/** Inert Memory destinations so the tiles' RouterLinks resolve without mounting the app. */
export function memoryRouterRoutes(): RouteRecordRaw[] {
  return [{
    path: '/memory/:section?',
    component: defineComponent({ render: () => h('p', 'Inert Memory destination for workshop routing only.') }),
  }]
}

// ── Connected mounts ─────────────────────────────────────────────────────────

/**
 * Mounts the production MemoryGuideBudget owner into the preview's boundary in
 * the review sections' real rail host, with the app's toast stack (Open and
 * Discuss act on app-level surfaces; Discuss with no General project records
 * its fail-closed toast there).
 */
export function mountGuideFixture(host: HTMLElement, component: Component): ConnectedFixture {
  return mountConnected(host, {
    component: defineComponent({
      setup() {
        return () => h('div', [
          // The app renders the toast stack at its root; Discuss with no
          // General project records its fail-closed error toast there.
          h(InAppToast),
          h('div', { style: 'container-type: inline-size; container-name: chat-pane' }, [
            h('div', { class: 'page-grid' }, [
              h('div', { class: 'page-main' }, [
                h('p', { style: 'padding: var(--space-2)' }, 'Synthetic main column: the always-loaded rail hosts the guide budget in the review sections.'),
              ]),
              h('aside', { class: 'page-rail', 'aria-label': 'Always loaded' }, [h(component)]),
            ]),
          ]),
        ])
      },
    }),
    seed: (pinia) => {
      useProjectStore(pinia).activeWorkspace = guideWorkspace
    },
  })
}
