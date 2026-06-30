import { createRouter, createWebHistory } from 'vue-router'

const routes = [
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
    path: '/schedules',
    name: 'schedules',
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
