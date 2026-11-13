<template>
  <section class="terminal-path" :aria-label="`${target} terminal PATH`">
    <p class="terminal-path-title">Terminal PATH · {{ target }}</p>
    <div ref="result" tabindex="-1">
      <p v-if="error" role="alert" class="hint hint--warn">{{ message }}</p>
      <p v-else role="status" aria-live="polite" :aria-label="`${target} terminal PATH status`" class="hint">{{ message }}</p>
      <p v-if="warning" class="hint">{{ warning }}</p>
    </div>
    <template v-if="proposal">
      <p class="hint">Executable: <code>{{ proposal.executable }}</code><br>Directory: <code>{{ proposal.directory }}</code><br>Destination: <code>{{ proposal.destination }}</code></p>
      <p class="hint">Append this directory to your terminal PATH at the destination above. Open a new terminal for the change to take effect.</p>
      <button ref="approval" type="button" class="btn-primary btn-small" @click="apply">Add to terminal PATH</button>
    </template>
    <button v-if="retry" type="button" class="btn-small" @click="preview">Check again</button>
  </section>
</template>

<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../../lib/api'

type Target = 'ciao' | 'claude' | 'opencode'
type Preview = { status: 'ready' | 'already_configured' | 'refused'; detail: string; executable: string; directory: string; destination: string; revision: string }
type Apply = { status: 'applied' | 'already_configured' | 'refused' | 'stale' | 'write_failed' | 'verification_failed'; detail: string; mutation: 'none' | 'partial' | 'unknown' | 'written'; warning: string }
const props = defineProps<{ target: Target }>()
const proposal = ref<Preview | null>(null)
const message = ref('Checking terminal PATH…')
const warning = ref('')
const error = ref(false)
const retry = ref(false)
const approval = ref<HTMLButtonElement | null>(null)
const result = ref<HTMLElement | null>(null)
let sequence = 0
let applying = false

function payload(e: unknown): Record<string, unknown> {
  if (e && typeof e === 'object' && 'payload' in e && e.payload && typeof e.payload === 'object') return e.payload as Record<string, unknown>
  return {}
}
function explain(e: unknown): string {
  const data = payload(e)
  if (data.error === 'not_on_host') return 'Open Settings on the computer running Ciaobot to change its terminal PATH.'
  if (typeof data.detail === 'string' && data.detail) return data.detail
  if (typeof data.error === 'string' && data.error) return data.error
  return 'Could not check terminal PATH. Try again.'
}
function validPreview(value: Preview): boolean {
  return !!value && ['ready', 'refused', 'already_configured'].includes(value.status) && typeof value.detail === 'string' && ['executable', 'directory', 'destination', 'revision'].every(k => typeof value[k as keyof Preview] === 'string') && (value.status !== 'ready' || (!!value.executable && !!value.directory && !!value.destination && /^[0-9a-f]{64}$/.test(value.revision)))
}
function validApply(value: Apply): boolean {
  if (!value || typeof value.detail !== 'string' || typeof value.warning !== 'string') return false
  const allowed: Record<Apply['status'], Apply['mutation'][]> = { applied: ['written'], already_configured: ['none'], refused: ['none'], stale: ['none'], write_failed: ['none', 'partial', 'unknown'], verification_failed: ['written'] }
  return !!allowed[value.status]?.includes(value.mutation)
}
async function preview() {
  const ticket = ++sequence
  const previousFailure = error.value ? message.value : ''
  const previousWarning = warning.value
  proposal.value = null
  retry.value = false
  error.value = false
  warning.value = ''
  message.value = 'Checking terminal PATH…'
  try {
    const data = await api.post<Preview>('/api/settings/terminal-path/preview', { target: props.target })
    if (ticket !== sequence) return
    if (!validPreview(data)) throw new Error('Invalid preview')
    message.value = data.detail || (data.status === 'already_configured' ? 'Already configured.' : 'Review the proposed change.')
    if (data.status === 'ready') proposal.value = data
    else retry.value = data.status === 'refused'
  } catch (e) {
    if (ticket !== sequence) return
    message.value = previousFailure ? `${previousFailure} ${explain(e)}` : explain(e)
    warning.value = previousWarning
    error.value = true
    retry.value = true
  }
}
async function apply() {
  if (!proposal.value || applying) return
  const { revision } = proposal.value
  const target = props.target
  const heldFocus = document.activeElement === approval.value
  const ticket = ++sequence
  applying = true
  proposal.value = null
  message.value = 'Applying terminal PATH change…'
  error.value = false
  retry.value = false
  warning.value = ''
  const request = api.post<Apply>('/api/settings/terminal-path/apply', { target, revision })
  void nextTick(() => { if (ticket === sequence && heldFocus && document.activeElement === document.body) result.value?.focus() })
  try {
    let data: Apply
    try { data = await request }
    catch (e) {
      const body = payload(e) as Apply
      if (!validApply(body) || body.status === 'applied' || body.status === 'already_configured') throw e
      data = body
    }
    if (ticket !== sequence) return
    if (!validApply(data)) throw new Error('Invalid apply response')
    warning.value = data.warning
    error.value = !['applied', 'already_configured'].includes(data.status)
    message.value = data.detail
    if (data.status === 'applied') message.value += ' Open a new terminal to use the change.'
    if (data.status === 'write_failed') message.value += data.mutation === 'none' ? ' No change was written.' : ' Configuration may have changed; inspect it before trying again.'
    if (data.status === 'verification_failed') message.value += ' The change was written but could not be verified; inspect the configuration.'
    retry.value = error.value
  } catch (e) {
    if (ticket !== sequence) return
    error.value = true
    const code = payload(e).error
    const reason = ['not_on_host', 'forbidden', 'invalid_request', 'unauthorized', 'internal_error'].includes(String(code))
      ? explain({ payload: { error: code } }) : 'The change could not be confirmed.'
    message.value = `${reason} Configuration may have changed; inspect it before trying again.`
    retry.value = true
  } finally { if (ticket === sequence) applying = false }
}
watch(() => props.target, () => { applying = false; void preview() }, { immediate: true })
onBeforeUnmount(() => { sequence++ })
</script>

<style scoped>
.terminal-path { margin-top: 16px; min-width: 0; }
.terminal-path-title { font-weight: 600; }
.terminal-path code { overflow-wrap: anywhere; }
.terminal-path button { margin-top: 8px; }
@media (pointer: coarse) { .terminal-path button { min-height: 44px; min-width: 44px; } }
</style>
