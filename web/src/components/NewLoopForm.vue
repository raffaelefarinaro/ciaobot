<template>
  <form class="new-form" @submit.prevent="submit">
    <p class="hint">
      A loop re-sends the same prompt into one existing chat every N minutes.
      It runs with whatever model the chat is set to — change the chat's model to change the loop's.
    </p>
    <div class="form-grid">
      <div class="form-group">
        <label>Every (minutes)</label>
        <input v-model.number="intervalMinutes" type="number" min="1" required />
      </div>
      <div class="form-group">
        <label>Chat</label>
        <select v-model="webChatId" required>
          <option value="" disabled>Pick a chat…</option>
          <optgroup v-for="group in chatGroups" :key="group.label" :label="group.label">
            <option v-for="c in group.items" :key="c.key" :value="c.key">
              {{ c.label }}
            </option>
          </optgroup>
        </select>
      </div>
    </div>
    <div class="form-group">
      <label>Title <span class="optional">(optional)</span></label>
      <input v-model="title" type="text" placeholder="e.g. PR watcher" />
    </div>
    <div class="form-group">
      <label>Prompt</label>
      <textarea v-model="prompt" placeholder="e.g. Check my open PRs for new reviews or CI failures. If nothing changed, reply with just 'no changes'." rows="3" required></textarea>
    </div>
    <label class="checkbox-line">
      <input v-model="autostart" type="checkbox" />
      Start with the server (otherwise the loop stays stopped after a restart until started manually)
    </label>
    <label class="checkbox-line">
      <input v-model="startNow" type="checkbox" />
      Start running immediately
    </label>
    <button class="btn-primary" :disabled="!prompt || !webChatId || !intervalMinutes || intervalMinutes < 1">Create Loop</button>
  </form>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useTaskStore } from '../stores/tasks'
import { useProjectStore } from '../stores/projects'

const emit = defineEmits<{ created: [] }>()
const store = useTaskStore()
const projectStore = useProjectStore()

const prompt = ref('')
const title = ref('')
const webChatId = ref('')
const intervalMinutes = ref(10)
const autostart = ref(false)
const startNow = ref(true)

const chatGroups = computed(() => {
  const groups: { label: string; items: { key: string; label: string }[] }[] = []
  for (const p of projectStore.projects) {
    const items = projectStore.projectChats(p.project_id).map(c => ({
      key: c.chat_id,
      label: c.title,
    }))
    if (items.length) groups.push({ label: `${p.name} (${p.workspace})`, items })
  }
  return groups
})

async function submit() {
  await store.createLoop({
    prompt: prompt.value,
    web_chat_id: webChatId.value,
    interval_minutes: intervalMinutes.value,
    title: title.value || undefined,
    autostart: autostart.value,
    start: startNow.value,
  })
  prompt.value = ''
  title.value = ''
  webChatId.value = ''
  intervalMinutes.value = 10
  autostart.value = false
  startNow.value = true
  emit('created')
}
</script>

<style scoped>
.new-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 12px;
  padding: 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
}

.hint { font-size: var(--text-xs); color: var(--fg2); margin: 0; }
.optional { color: var(--fg3); font-weight: 400; }

.checkbox-line {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--text-sm);
  color: var(--fg2);
  cursor: pointer;
}
.checkbox-line input { flex-shrink: 0; }

textarea { resize: vertical; min-height: 60px; }
</style>
