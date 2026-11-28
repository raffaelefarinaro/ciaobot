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
    path: '/project/:projectId',
    name: 'project',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    // The Automations destination: Schedules keeps /schedules (and an open
    // automation /schedules/:scheduleId); Loops and Webhooks are their own
    // pages under /automations/, where no schedule id can collide with them
    // (lib/automationsNav.ts). Bare /automations lands on Schedules.
    path: '/automations',
    redirect: '/schedules',
  },
  {
    path: '/automations/loops',
    name: 'automations-loops',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/automations/webhooks',
    name: 'automations-webhooks',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
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
    path: '/memory/:section(map|categories|unsure|skills|care)?',
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
    // The Usage page: a read-only look back; reached from the sidebar's Usage entry and Home's At a glance.
    path: '/stats',
    name: 'stats',
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
  {
    // An address no view owns (an old link, a typo such as /usage) lands on
    // Home instead of rendering an empty shell.
    path: '/:pathMatch(.*)*',
    redirect: '/',
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
