import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('./components/LoginView.vue'),
  },
  {
    // This machine, not the host it mirrors: role, host connection, local
    // install. Deliberately outside the auth guard and outside ChatLayout —
    // it is the way out of client mode, so it must load when the host (and
    // with it every proxied API call) is unreachable.
    path: '/device',
    name: 'device',
    component: () => import('./components/DeviceView.vue'),
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
    path: '/memory',
    name: 'memory',
    component: () => import('./components/ChatLayout.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/proposals',
    name: 'proposals',
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
      return { name: 'login' }
    }
  }
})
