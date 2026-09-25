import { computed, inject, ref, type Ref } from 'vue'
import { getActivePinia } from 'pinia'
import { routerKey, type Router } from 'vue-router'
import { useProjectStore } from '../stores/projects'
import { useTaskStore } from '../stores/tasks'

// Back / forward through Ciaobot's own navigation. vue-router keeps the
// neighbouring entries in `history.state.back` / `.forward`, so the buttons know
// whether there is anywhere to go and where, without a stack of our own. A
// `back` of null is the first in-app entry: going back from there would leave
// the app (or do nothing in the desktop shell), so the button is disabled.

interface HistoryEdges { back: string | null, forward: string | null }

const edges: Ref<HistoryEdges> = ref({ back: null, forward: null })
let installedOn: Router | null = null

function readEdges(): HistoryEdges {
  const state = (typeof window !== 'undefined' ? window.history.state : null) as Partial<HistoryEdges> | null
  return {
    back: typeof state?.back === 'string' ? state.back : null,
    forward: typeof state?.forward === 'string' ? state.forward : null,
  }
}

function install(router: Router): void {
  if (installedOn === router) return
  installedOn = router
  edges.value = readEdges()
  // afterEach runs once the history entry is written, so state is current.
  router.afterEach(() => { edges.value = readEdges() })
}

const SECTION_LABELS: Record<string, string> = {
  '/': 'Home',
  '/schedules': 'Automations',
  '/memory': 'Memory',
  '/memory/suggested': 'Suggested memories',
  '/memory/revisit': 'Notes to revisit',
  '/memory/map': 'Memory map',
  '/memory/retired': 'Retired notes',
  '/memory/history': 'Memory history',
  '/settings': 'Settings',
}

export function useHistoryNav() {
  // Both are optional so a bare component mount (no router, no pinia) renders
  // with the buttons disabled instead of throwing.
  const router = inject(routerKey, null)
  if (router) install(router)

  // A short name for the page a path leads to, for the buttons' tooltips.
  function describe(path: string | null): string | null {
    if (!path) return null
    if (!getActivePinia()) return null
    const store = useProjectStore()
    const tasks = useTaskStore()
    const clean = path.split(/[?#]/)[0] || '/'
    const [, first = '', id = ''] = clean.split('/')
    if (first === 'chat' && id) {
      return store.chats.find(c => c.chat_id === decodeURIComponent(id))?.title || 'Chat'
    }
    if (first === 'project' && id) {
      return store.projects.find(p => p.project_id === decodeURIComponent(id))?.name || 'Project'
    }
    if (first === 'schedules' && id) {
      return tasks.schedules.find(s => s.schedule_id === decodeURIComponent(id))?.title || 'Automation'
    }
    if (first === 'settings') return 'Settings'
    return SECTION_LABELS[clean] ?? null
  }

  const canBack = computed(() => !!router && edges.value.back !== null)
  const canForward = computed(() => !!router && edges.value.forward !== null)
  const backLabel = computed(() => (canBack.value ? `Back to ${describe(edges.value.back) ?? 'previous page'}` : 'Back'))
  const forwardLabel = computed(() => (canForward.value ? `Forward to ${describe(edges.value.forward) ?? 'next page'}` : 'Forward'))

  return {
    canBack,
    canForward,
    backLabel,
    forwardLabel,
    back: () => { if (canBack.value) router?.back() },
    forward: () => { if (canForward.value) router?.forward() },
  }
}
