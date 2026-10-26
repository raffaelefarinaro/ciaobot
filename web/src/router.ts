import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
  {
    path: '/onboarding',
    name: 'onboarding',
    component: () => import('./components/onboarding/OnboardingView.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/login',
    name: 'login',
    component: () => import('./components/LoginView.vue'),
  },
  {
    path: '/',
    name: 'chat',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/chat/:chatId?',
    name: 'chat-detail',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // Read-only view of one subagent's own conversation. Nested under the chat
    // that spawned it because that is the only place the transcript exists —
    // a Claude Code subagent is a transcript file, not a resumable session.
    path: '/chat/:chatId/subagent/:agentId',
    name: 'chat-subagent',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/project/:projectId',
    name: 'project',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // The Schedules page grew into "Automations" (schedules + loops); keep
    // the /schedules paths as canonical and alias /automations onto them.
    path: '/automations',
    redirect: '/schedules',
  },
  {
    path: '/schedules',
    name: 'schedules',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // One route per memory section, listed in the sidebar like Settings'
    // tabs. Bare /memory lands on the last section visited (MemoryMapView).
    path: '/memory/:section(review|map|categories|import|retired|history)?',
    name: 'memory',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // The workspace task board. Its pane is a `viewMode` branch in
    // ChatLayout like every other destination; this record only names the
    // address and the auth gate.
    path: '/tasks',
    name: 'tasks',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // The notification inbox. Its pane is a `viewMode` branch in ChatLayout
    // like every other destination; this record only names the address and
    // the auth gate.
    path: '/notifications',
    name: 'notifications',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // One task, open in the board's detail dialog: the address an agent links
    // to (lib/appLinks.ts) and the one the dialog keeps while it is open.
    path: '/tasks/:taskId',
    name: 'task-detail',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // The proposal queue's old address; To decide is where it lives now.
    path: '/proposals',
    redirect: '/memory/review?show=suggested',
  },
  {
    path: '/schedules/:scheduleId',
    name: 'schedule-detail',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // The Models & providers tab was renamed Providers. Old links and bookmarks keep working.
    path: '/settings/models',
    redirect: to => ({ path: '/settings/providers', query: to.query, hash: to.hash }),
  },
  {
    path: '/settings/:tab',
    name: 'settings-tab',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to) => {
  if (to.meta.requiresAuth) {
    const { useAuthStore } = await import('./stores/auth')
    const auth = useAuthStore()
    if (!auth.authenticated) {
      await auth.check()
    }
    if (!auth.authenticated) {
      return { name: 'login', query: { ...to.query, returnTo: to.fullPath } }
    }
  }
})
