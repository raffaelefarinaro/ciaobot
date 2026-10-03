import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

export const routes: RouteRecordRaw[] = [
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
    // The providers tab folded into models (chat providers are its first
    // card). Keep old links and bookmarks working.
    path: '/settings/providers',
    redirect: { path: '/settings/models', hash: '#chat-providers' },
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
      return { name: 'login', query: to.query.shared ? { shared: to.query.shared } : {} }
    }
  }
})
