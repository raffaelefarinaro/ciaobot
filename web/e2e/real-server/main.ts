import { createApp, h, ref } from 'vue'
import { createPinia } from 'pinia'
import { useProposalsStore } from '../../src/stores/proposals'

createApp({
  setup() {
    const store = useProposalsStore()
    const inspected = ref(false)
    const inspection = ref('')
    void store.fetch({ force: true })
    return () => {
      const row = store.rows[0]
      const id = row?.id || Object.keys(store.partialWarnings)[0]
      return h('div', [
        h('p', { 'data-testid': 'ready' }, row ? 'ready' : 'loading'),
        h('p', { 'data-testid': 'warning' }, id && store.partialWarnings[id] ? 'unresolved' : 'clear'),
        h('p', { 'data-testid': 'lock' }, store.intentLockError),
        h('p', { 'data-testid': 'inspection' }, inspection.value),
        h('button', { 'data-testid': 'act', onClick: () => id && store.act(id, 'accept') }, 'Accept'),
        h('button', { 'data-testid': 'inspect', onClick: async () => {
          const response = await fetch('/__proposal_test__/status')
          inspection.value = JSON.stringify(await response.json())
          inspected.value = true
        } }, 'Inspect queue'),
        h('button', { 'data-testid': 'ack', disabled: !inspected.value,
          onClick: () => id && store.acknowledgePartial(id) }, 'Acknowledge inspection'),
      ])
    }
  },
}).use(createPinia()).mount('#app')
