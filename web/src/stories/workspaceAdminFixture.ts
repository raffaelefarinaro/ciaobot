import { defineComponent, h, onMounted, provide, ref, type Component } from 'vue'
import { useWorkspaceAdmin, WORKSPACE_ADMIN_KEY } from '../composables/useWorkspaceAdmin'
import { ownerPanel, seedWorkspace } from './settingsAccessFixture'

export function workspacePanel(component: Component, archived = false) {
  const host = defineComponent({
    setup() {
      const admin = useWorkspaceAdmin({ notifySaved: () => {}, notifyFailed: () => {} })
      provide(WORKSPACE_ADMIN_KEY, { ...admin, settingsMenu: ref(null), setSettingsMenu: () => {}, closeSettingsMenuOnEscape: () => {} })
      onMounted(() => {
        void admin.fetchWorkspacesList()
        if (archived) void admin.fetchArchivedWorkspaces()
      })
      return () => h(component)
    },
  })
  return ownerPanel({ label: 'workspace admin', component: host, seed: seedWorkspace })
}
