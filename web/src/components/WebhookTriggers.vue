<template>
  <section class="ov-section" aria-labelledby="wh-section-title">
    <div class="ov-head">
      <h2 id="wh-section-title">Webhook triggers</h2>
      <span class="ov-hint">other tools can post an event here</span>
      <!-- `showFormArea`, not merely "no error": a toggle that opened nothing while
           the first read is still in flight, or beside a refusal, is a control
           that lies about what it does. Retry is the whole answer in those two
           states. -->
      <button
        v-if="showFormArea"
        ref="newTriggerBtn"
        type="button"
        class="btn-small ov-run-all"
        :aria-expanded="showForm"
        @click="toggleForm"
      >{{ showForm ? 'Cancel' : 'New trigger' }}</button>
    </div>

    <!-- First load: a pending question, not an empty answer. It replaces the
         list rather than sitting above it, because there is no list yet and an
         empty one beside "Loading…" would be two answers to one question. -->
    <p v-if="store.loading && !store.loaded" class="ov-empty" role="status">
      Loading webhook triggers…
    </p>

    <!-- First load failed. An empty list would claim the workspace has none.
         The two error states are siblings rather than one branch, because only
         the first-load case has nothing to keep: a failed refresh draws its
         banner *above* the rows it is keeping. -->
    <div v-if="store.loadError && !store.loaded" class="wh-error" role="alert">
      <p>Could not load webhook triggers. {{ store.loadError }}</p>
      <button type="button" class="btn-small" :disabled="store.loading" @click="reload">
        {{ store.loading ? 'Retrying…' : 'Retry' }}
      </button>
    </div>
    <div v-else-if="store.loadError" class="wh-stale" role="status">
      <span>Could not refresh webhook triggers. {{ store.loadError }} Showing the last successful load.</span>
      <button type="button" class="btn-small" :disabled="store.loading" @click="reload">
        {{ store.loading ? 'Retrying…' : 'Retry' }}
      </button>
    </div>

    <!-- Gated on the first load being *settled*, not on it having succeeded: the
         create form is how a workspace with no triggers gets one, so it must
         survive an empty list. It is withheld only while the read is in flight
         and on a failed one, where Retry is the whole answer. -->
    <template v-if="showFormArea">
      <form v-if="showForm" class="wh-form" @submit.prevent="submitCreate">
        <label class="wh-field">
          <span class="wh-label">Name</span>
          <input
            v-model="draft.name"
            class="wh-input"
            type="text"
            maxlength="120"
            autocomplete="off"
            :disabled="store.saving"
            placeholder="e.g. New issue from GitHub"
          />
        </label>
        <label class="wh-field">
          <span class="wh-label">Project</span>
          <select v-model="draft.projectId" class="wh-input" :disabled="store.saving">
            <option value="">General (this workspace)</option>
            <option v-for="p in workspaceProjects" :key="p.project_id" :value="p.project_id">
              {{ p.name }}
            </option>
          </select>
        </label>
        <label class="wh-field wh-field--wide">
          <span class="wh-label">Instructions</span>
          <textarea
            v-model="draft.instructions"
            class="wh-input wh-textarea"
            rows="3"
            :disabled="store.saving"
            placeholder="What the trigger should do with each event. Optional."
          />
        </label>
        <label class="wh-field">
          <span class="wh-label">Mode</span>
          <select v-model="draft.mode" class="wh-input" :disabled="store.saving">
            <option v-for="option in MODE_OPTIONS" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </select>
        </label>
        <!-- Mode and project are said here or nowhere: the row has no Edit, and both
             are create-only on the route, so this form is the only place the
             choice is ever explained. -->
        <p class="wh-hint wh-field--wide">{{ modeHint }} Project and mode cannot be changed after the trigger is created.</p>
        <div class="wh-actions wh-field--wide">
          <button type="button" class="btn-small" :disabled="store.saving" @click="closeForm">
            Cancel
          </button>
          <button
            type="submit"
            class="btn-small btn-primary"
            :disabled="store.saving || !draft.name.trim()"
          >{{ store.saving ? 'Creating…' : 'Create trigger' }}</button>
        </div>
      </form>

      <!-- A write's own refusal: the server's sentence, with the rows intact. A
           stale revision is the one refusal the reader most needs to see, so it
           is never collapsed into a generic failure. -->
      <div v-if="store.error" class="wh-error" role="alert">
        <span>{{ store.error }}</span>
        <button type="button" class="btn-small" @click="store.clearError()">Dismiss</button>
      </div>

      <div
        v-for="t in store.triggers"
        :key="t.trigger_id"
        class="ov-item wh-row"
        :class="{ 'ov-item--paused': !t.enabled }"
      >
        <span class="ov-main">
          <!-- `title` on the name: `.ov-title` ellipsizes in a narrow pane, and
               a trigger's name is the one thing a row cannot be read without. -->
          <span class="ov-title" :title="t.name">{{ t.name }}</span>
          <span class="ov-sub">{{ rowSummary(t) }}</span>
        </span>
        <!-- The state badge and the controls share one right-hand group, so they
             wrap together to a second line instead of leaving the badge stranded
             on the first. The badge is its own flex item rather than sitting
             inside the title, where an ellipsized span would swallow it exactly
             at the widths where the state is hardest to read. -->
        <span class="wh-row-side">
          <span class="badge wh-state" :class="t.enabled ? 'badge--success' : 'badge--muted'">
            {{ t.enabled ? 'Enabled' : 'Disabled' }}
          </span>
          <span class="wh-row-actions">
            <button
              type="button"
              class="btn-small"
              :disabled="store.saving"
              @click="toggleEnabled(t)"
            >{{ pendingId === t.trigger_id ? 'Saving…' : (t.enabled ? 'Disable' : 'Enable') }}</button>
            <button
              type="button"
              class="btn-small"
              @click="toggleRecipe(t)"
            >{{ recipeLabelFor(t.trigger_id) }}</button>
            <button
              type="button"
              class="btn-small"
              :disabled="store.saving"
              :title="`Replace the secret ${t.name} uses. Senders must update theirs.`"
              @click="rotate(t)"
            >{{ pendingId === t.trigger_id ? 'Rotating…' : 'Rotate' }}</button>
            <!-- Delete is destructive and rare, so it is behind the row's menu
                 rather than a red control on every row. -->
            <DropdownMenuRoot :modal="false">
              <DropdownMenuTrigger as-child>
                <button
                  type="button"
                  class="btn-icon wh-overflow"
                  :aria-label="`More actions for ${t.name}`"
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                    <circle cx="5" cy="12" r="1.6" /><circle cx="12" cy="12" r="1.6" /><circle cx="19" cy="12" r="1.6" />
                  </svg>
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuPortal>
                <DropdownMenuContent as-child align="end" :side-offset="6" :collision-padding="8">
                  <div class="wh-menu">
                    <DropdownMenuItem as-child>
                      <button type="button" class="wh-menu-item danger" @click="remove(t)">
                        Delete trigger…
                      </button>
                    </DropdownMenuItem>
                  </div>
                </DropdownMenuContent>
              </DropdownMenuPortal>
            </DropdownMenuRoot>
          </span>
        </span>
      </div>

      <p v-if="!store.triggers.length" class="ov-empty">
        No webhook triggers yet. One lets another tool post an event straight into
        this workspace — create it, then hand the sender the secret it shows once.
        New triggers start disabled. Press Enable on the row when the sender is ready.
      </p>
    </template>

    <!-- The receiver recipe: the machine contract, with this trigger's real id.
         The secret is not in it — see lib/webhooks.ts.

         Portalled to `body`, which is not decoration: this section is drawn inside
         `.chat-main`, and that pane declares `container-type: inline-size`, which
         makes it the containing block for `position: fixed` descendants. An
         in-place overlay would dim and clip to the Automations pane and leave the
         sidebar live. `as-child` keeps the backdrop this template's own element, so
         the scoped styles still reach it. -->
    <DialogRoot :open="recipeId !== ''" modal @update:open="onRecipeOpenChange">
      <DialogPortal>
        <DialogOverlay as-child>
          <div class="wh-backdrop">
            <DialogContent
              class="wh-card"
              aria-modal="true"
              @open-auto-focus="onRecipeAutoFocus"
              @escape-key-down="onRecipeEscape"
            >
              <DialogTitle as="p" class="wh-card-title">Receiver recipe</DialogTitle>
              <DialogDescription as="p" class="wh-card-text">
                Post this to Ciaobot to record an event for
                <strong>{{ recipeName }}</strong>. Send
                <code>&lt;secret&gt;</code> as the bearer — the trigger's secret, which
                the one-time dialog showed you and this page will not show again.
                Use a fresh <code>Idempotency-Key</code> per event: the same key with a
                different body is refused.
              </DialogDescription>
              <pre class="wh-recipe"><code>{{ recipeText }}</code></pre>
              <div class="wh-card-actions">
                <DialogClose as-child>
                  <button type="button" class="wh-action wh-action--cancel">Done</button>
                </DialogClose>
                <button ref="recipeCopyBtn" type="button" class="wh-action wh-action--primary" @click="copyRecipe">
                  {{ recipeCopyLabel }}
                </button>
              </div>
            </DialogContent>
          </div>
        </DialogOverlay>
      </DialogPortal>
    </DialogRoot>

    <!-- The one and only place a raw secret is ever shown. `secret` is a plain
         ref rather than a store field, cleared when the dialog closes, so no
         later render, list refresh or log can echo a credential the engine only
         sends once.

         Portalled for the same reason as the recipe above, and closed on Done or
         Escape only: a backdrop click that dismissed this would throw away the
         one copy of a credential the engine will not send again, and the user's
         only recovery would be a rotation they did not know they needed. -->
    <DialogRoot :open="revealedSecret !== ''" modal @update:open="onSecretOpenChange">
      <DialogPortal>
        <DialogOverlay as-child>
          <div class="wh-backdrop">
            <DialogContent
              class="wh-card"
              aria-modal="true"
              @open-auto-focus="onSecretAutoFocus"
              @escape-key-down="onSecretEscape"
              @pointer-down-outside.prevent
              @interact-outside.prevent
            >
              <DialogTitle as="p" class="wh-card-title">
                {{ revealed?.rotated ? 'The old secret is dead' : 'Copy this secret now' }}
              </DialogTitle>
              <DialogDescription as="p" class="wh-card-text">
                <template v-if="revealed?.rotated">
                  The previous secret for <strong>{{ revealed.name }}</strong> stopped
                  working just now. Update every sender that uses it, or they will be
                  refused.
                </template>
                <template v-else>
                  This is the only time Ciaobot will show the secret for
                  <strong>{{ revealed?.name }}</strong>. There is no copy you can read
                  again — if you lose it, rotate the trigger to get a new one. New
                  triggers start disabled, so a sender posts a 401 until you press
                  Enable on the row.
                </template>
              </DialogDescription>
              <p class="wh-warning">Shown once.</p>
              <pre class="wh-recipe"><code>{{ revealedSecret }}</code></pre>
              <div class="wh-card-actions">
                <DialogClose as-child>
                  <button type="button" class="wh-action wh-action--cancel" @click="closeSecret">
                    Done
                  </button>
                </DialogClose>
                <button ref="secretCopyBtn" type="button" class="wh-action wh-action--primary" @click="copySecret">
                  {{ secretCopyLabel }}
                </button>
              </div>
            </DialogContent>
          </div>
        </DialogOverlay>
      </DialogPortal>
    </DialogRoot>
  </section>
</template>

<script setup lang="ts">
import {
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogOverlay,
  DialogPortal,
  DialogRoot,
  DialogTitle,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useProjectStore } from '../stores/projects'
import { useWebhookStore } from '../stores/webhooks'
import { writeClipboard } from '../lib/codeCopy'
import { askConfirm } from '../lib/confirm'
import { formatRelative } from '../lib/time'
import { webhookModeHint, webhookModeLabel, webhookRecipe } from '../lib/webhooks'
import type { WebhookMode, WebhookTrigger } from '../lib/types'

/** The store's `WEBHOOK_MODES`, with the labels the create form shows. */
const MODE_OPTIONS: { value: WebhookMode; label: string }[] = [
  { value: 'auto', label: 'Auto' },
  { value: 'normal', label: 'Normal' },
  { value: 'plan', label: 'Plan' },
]

const store = useWebhookStore()
const projectStore = useProjectStore()

const workspace = computed(() => projectStore.activeWorkspace)
const workspaceProjects = computed(() =>
  projectStore.projects.filter(p => p.workspace === workspace.value),
)

/** A first load that failed has no rows to keep and no honest list to sit under. */
const firstLoadFailed = computed(() => Boolean(store.loadError) && !store.loaded)

/**
 * Whether the form and the list belong on screen at all.
 *
 * Not `store.loaded`: a read that succeeded with no triggers is exactly the
 * state a user creates one from, so gating on "has a list" would make an empty
 * workspace unreachable. What this withholds is the two states where there is
 * nothing to draw against — a first read in flight, and a first read that
 * failed, where Retry is the only honest action.
 */
const showFormArea = computed(() => store.loaded || (!store.loading && !firstLoadFailed.value))

const showForm = ref(false)
const draft = ref({ name: '', projectId: '', instructions: '', mode: 'auto' as WebhookMode })
const modeHint = computed(() => webhookModeHint(draft.value.mode))
/** The one trigger whose write is in flight, so only its row reports progress. */
const pendingId = ref('')

/** `trigger_id` of the trigger whose recipe is open, or `''` for none. */
const recipeId = ref('')
/**
 * The copy confirmation, as a key and whether it worked.
 *
 * One slot for both dialogs rather than two, because they are never open at the
 * same time — the secret dialog is modal, so the recipe behind it cannot be
 * copied from until that one closes. A key rather than a boolean, so a flash for
 * one control never re-labels the other.
 */
const copyState = ref<{ key: string; ok: boolean } | null>(null)
let copyTimer: number | undefined

/** The one-time secret, held here and only here, and only while its dialog is open. */
const revealedSecret = ref('')
/** The trigger the secret belongs to, and whether a rotation retired a previous one. */
const revealed = ref<{ name: string; rotated: boolean } | null>(null)

/*
 * The controls focus is handed to, none of which Reka can choose on its own.
 *
 * Each dialog cancels the primitive's own first-focus choice and places its own
 * after the render tick, the way `PromptDialog.vue` does with its input: the
 * choice is a *control*, not the first focusable thing in the card.
 */
const newTriggerBtn = ref<HTMLButtonElement | null>(null)
const secretCopyBtn = ref<HTMLButtonElement | null>(null)
const recipeCopyBtn = ref<HTMLButtonElement | null>(null)

const recipeName = computed(
  () => store.triggers.find(t => t.trigger_id === recipeId.value)?.name || 'this trigger',
)
const recipeText = computed(() => webhookRecipe(recipeId.value))

onMounted(() => {
  void store.ensureLoaded(workspace.value)
})

// A `1`–`9` shortcut can move the pane to another workspace while a read is in
// flight. The store drops a late answer that is not the workspace it was asked
// for; this closes the form and the dialogs too, so nothing survives holding a
// destination that belongs to the workspace just left.
watch(workspace, () => {
  showForm.value = false
  closeRecipe()
  closeSecret()
  void store.ensureLoaded(workspace.value)
})

function reload() {
  void store.reload(workspace.value)
}

function rowSummary(t: WebhookTrigger): string {
  const target = t.project_id
    ? projectStore.projects.find(p => p.project_id === t.project_id)?.name || 'a missing project'
    : 'General'
  return [
    target,
    `${webhookModeLabel(t.mode)} mode`,
    `updated ${formatRelative(t.updated_at) || 'never'}`,
  ].join(' · ')
}

// ── The create form ────────────────────────────────────────────────────────

function toggleForm() {
  showForm.value = !showForm.value
  if (showForm.value) {
    store.clearError()
    draft.value = { name: '', projectId: '', instructions: '', mode: 'auto' }
  }
}

function closeForm() {
  showForm.value = false
}

async function submitCreate() {
  if (!draft.value.name.trim()) return
  const created = await store.create({
    name: draft.value.name,
    workspace: workspace.value,
    instructions: draft.value.instructions,
    project_id: draft.value.projectId || null,
    mode: draft.value.mode,
  })
  // `null` is a refusal (the server's sentence is in `store.error`, the form stays
  // open with what was typed) or a workspace switch that overtook the POST.
  if (!created) return
  showForm.value = false
  reveal(created.secret, created.trigger.name, false)
}

// ── Row writes ──────────────────────────────────────────────────────────────

/**
 * The revision a row's action presents.
 *
 * The row's, never a value captured when the row was drawn: the store's list is
 * the newest read this pane holds, and the server refuses a write that presents
 * anything older with a 409 that writes nothing. Reading it here keeps every
 * write in this component presenting the same revision for the same row.
 */
function revisionFor(t: WebhookTrigger): number {
  return store.revisionOf(t.trigger_id) || t.revision
}

async function toggleEnabled(t: WebhookTrigger) {
  pendingId.value = t.trigger_id
  try {
    await store.update(workspace.value, t.trigger_id, revisionFor(t), { enabled: !t.enabled })
  } finally {
    pendingId.value = ''
  }
}

async function rotate(t: WebhookTrigger) {
  // One click retires a credential that is working right now, so it is asked for
  // first: the warning used to be a `title`, which no touch reader ever sees and
  // which arrives after the secret is already dead.
  const confirmed = await askConfirm(
    `Rotate the secret for “${t.name}”? Every sender using it is refused until it is updated with the new one.`,
    { title: 'Rotate this webhook secret?', confirmLabel: 'Rotate secret' },
  )
  if (!confirmed) return
  pendingId.value = t.trigger_id
  try {
    const result = await store.rotate(workspace.value, t.trigger_id, revisionFor(t))
    if (!result) return
    reveal(result.secret, result.trigger.name, true)
  } finally {
    pendingId.value = ''
  }
}

async function remove(t: WebhookTrigger) {
  const confirmed = await askConfirm(
    `Delete “${t.name}”? Its secret stops working at once and there is no way to read it again.`,
    { title: 'Delete this webhook trigger?', confirmLabel: 'Delete trigger', destructive: true },
  )
  if (!confirmed) return
  pendingId.value = t.trigger_id
  try {
    await store.remove(workspace.value, t.trigger_id, revisionFor(t))
  } finally {
    pendingId.value = ''
  }
}

// ── The one-time secret ────────────────────────────────────────────────────

function reveal(secret: string, name: string, isRotation: boolean) {
  revealedSecret.value = secret
  revealed.value = { name, rotated: isRotation }
}

/**
 * Close the secret dialog, and hand focus back to where it came from.
 *
 * The section's own "New trigger" button, but only after a create: that submit
 * unmounted the whole form, submit button included, so without this focus falls
 * to `BODY` and the next Tab restarts from the top of the document. After a
 * rotation the row is still there, so Reka's own focus return is the right one
 * and this leaves it alone.
 */
function closeSecret() {
  const afterCreate = revealed.value?.rotated === false
  revealedSecret.value = ''
  revealed.value = null
  if (afterCreate) void nextTick(() => newTriggerBtn.value?.focus())
}

function onSecretOpenChange(open: boolean) {
  if (!open) closeSecret()
}

/**
 * Where focus lands in the secret dialog: the copy button.
 *
 * Hand-placed rather than left to Reka, whose own choice is the first focusable
 * — `Done` here, the one control in the card that is *not* why it is open. The
 * secret itself is a `<pre>`: selectable text, not focusable, so nothing is lost
 * by skipping it. `preventDefault` is what keeps the primitive from undoing this
 * on its way out.
 */
function onSecretAutoFocus(event: Event) {
  event.preventDefault()
  void nextTick(() => secretCopyBtn.value?.focus())
}

function onSecretEscape(event: KeyboardEvent) {
  event.preventDefault()
  closeSecret()
}

async function copySecret() {
  const secret = revealedSecret.value
  if (!secret) return
  await flashCopy('secret', secret)
}

// ── The receiver recipe ────────────────────────────────────────────────────

/**
 * Open or close this trigger's recipe.
 *
 * One control rather than an open-only one, so the row that opened it can close
 * it again without the reader hunting for a dismiss — the same reason the row's
 * label changes with it.
 */
function toggleRecipe(t: WebhookTrigger) {
  if (recipeId.value === t.trigger_id) {
    closeRecipe()
    return
  }
  store.clearError()
  recipeId.value = t.trigger_id
}

function closeRecipe() {
  recipeId.value = ''
}

function onRecipeOpenChange(open: boolean) {
  if (!open) closeRecipe()
}

/**
 * Where focus lands in the recipe dialog: Copy recipe.
 *
 * The dialog exists to put that text on the clipboard, so the control that does
 * it is the first stop; `Done` only closes it.
 */
function onRecipeAutoFocus(event: Event) {
  event.preventDefault()
  void nextTick(() => recipeCopyBtn.value?.focus())
}

function onRecipeEscape(event: KeyboardEvent) {
  event.preventDefault()
  closeRecipe()
}

function recipeLabelFor(triggerId: string): string {
  return recipeId.value === triggerId ? 'Hide recipe' : 'Recipe'
}

async function copyRecipe() {
  const triggerId = recipeId.value
  if (!triggerId) return
  await flashCopy(`recipe:${triggerId}`, recipeText.value)
}

// ── Copy feedback ──────────────────────────────────────────────────────────

/**
 * Copy and flash the result on the button that asked for it.
 *
 * The confirmation is a word — `Copied` / `Copy failed` — and never a colour or
 * an icon alone: DESIGN.md asks for a text equivalent on every state. It reverts
 * on a timer, and only if the flash still belongs to this key, so a slow second
 * copy is not wiped by the first one's timer.
 */
async function flashCopy(key: string, text: string): Promise<void> {
  const copied = await writeClipboard(text)
  copyState.value = { key, ok: copied }
  if (copyTimer !== undefined) window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => {
    if (copyState.value?.key === key) copyState.value = null
  }, 1800)
}

/** A copy button's label: the flash while it holds, otherwise its own idle word. */
function copyLabel(key: string, idleLabel: string): string {
  if (copyState.value?.key !== key) return idleLabel
  return copyState.value.ok ? 'Copied' : 'Copy failed'
}

const recipeCopyLabel = computed(() => copyLabel(`recipe:${recipeId.value}`, 'Copy recipe'))
const secretCopyLabel = computed(() => copyLabel('secret', 'Copy secret'))
</script>

<style scoped src="./overviewSections.css"></style>
<style scoped>
.wh-error,
.wh-stale {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
  margin: 0 0 var(--space-2);
  padding: 9px 2px;
  color: var(--fg2);
  font-size: var(--text-sm);
}
.wh-error p { margin: 0; flex: 1; }
.wh-stale { color: var(--fg3); }
/* The row wraps rather than squeezing its name.
 *
 * A trigger's name is what identifies it, and four controls plus a state badge
 * are a lot to hold beside one. `.ov-title` ellipsizes inside whatever flex
 * space it is given, so a row that refuses to wrap shows `New issue fro…` at
 * exactly the pane widths where the name matters most. `flex-basis` on the text
 * block plus `margin-left: auto` on the actions is what decides the break: the
 * actions drop to their own full-width line once they would cost the name more
 * than its floor, and stay inline when there is room for both.
 */
.wh-row {
  flex-wrap: wrap;
  row-gap: var(--space-2);
}
.wh-row > .ov-main { flex: 1 1 16rem; }
/* The right-hand group: badge, then controls, wrapping as one unit. */
.wh-row-side {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-left: auto;
}
.wh-row-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--space-2);
}
.wh-overflow { color: var(--fg2); }
/* The state badge is a compact tag, not a count: the tight radius reserved for
   squared tags that must not be mistaken for a badge count. */
.wh-state { flex: none; border-radius: var(--radius-xs); align-self: center; }

/* Create form: the shared page form rhythm, capped to the column. */
.wh-form {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-3);
  padding: var(--space-3);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
}
.wh-field { display: flex; flex-direction: column; gap: var(--space-1); min-width: 0; }
.wh-field--wide { grid-column: 1 / -1; }
.wh-label {
  color: var(--fg2);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: var(--text-xs);
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}
.wh-input {
  min-height: 2.75rem;
  padding: 0 var(--space-2);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  background: var(--bg);
  color: var(--fg);
  font: inherit;
}
.wh-textarea { padding: var(--space-2); line-height: 1.45; }
.wh-input:focus-visible {
  border-color: var(--accent);
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.wh-input:disabled { opacity: 0.6; cursor: default; }
.wh-hint { margin: 0; color: var(--fg3); font-size: var(--text-sm); }
.wh-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: var(--space-2); }

/* Dialogs: the modal vocabulary from PromptDialog.vue / ConfirmDialog.vue. */
.wh-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: rgb(0 0 0 / 55%);
}
.wh-card {
  width: 100%;
  max-width: 34rem;
  max-height: min(90vh, 44rem);
  overflow: auto;
  padding: var(--space-4);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg);
  color: var(--fg);
  box-shadow: 0 1.5rem 3rem rgb(0 0 0 / 45%);
}
.wh-card-title { margin: 0 0 var(--space-2); font-weight: 600; }
.wh-card-text { margin: 0 0 var(--space-3); color: var(--fg2); line-height: 1.5; }
.wh-warning {
  margin: 0 0 var(--space-2);
  color: var(--warning);
  font-size: var(--text-sm);
  font-weight: 600;
}
/* The recipe is read and copied line by line, so it wraps rather than
   scrolling sideways: a sender pasting this must not have to scroll a `<pre>`
   to find out what the 202 body is. `overflow-wrap: anywhere` is the other half
   of that — a long token (a uuid, a trigger id) breaks instead of pushing the
   block wider than the dialog. */
.wh-recipe {
  margin: 0 0 var(--space-4);
  padding: var(--space-3);
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  background: var(--bg2);
  color: var(--fg);
  font-family: var(--font-mono, ui-monospace, monospace);
  font-size: var(--text-sm);
  line-height: 1.5;
  overflow-wrap: anywhere;
  user-select: text;
  white-space: pre-wrap;
}
.wh-card-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-2);
}
.wh-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 7rem;
  min-height: 2.75rem;
  padding: 0 var(--space-3);
  border: 1px solid transparent;
  border-radius: var(--radius);
  color: var(--fg);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
  transition:
    background 120ms var(--ease),
    border-color 120ms var(--ease),
    color 120ms var(--ease),
    transform 120ms var(--ease);
}
.wh-action:active { transform: scale(0.98); }
.wh-action--cancel { border-color: var(--border-strong); background: var(--bg3); }
.wh-action--cancel:hover { background: var(--border-strong); }
.wh-action--primary { border-color: var(--accent); background: var(--accent); color: var(--on-accent); }
.wh-action--primary:hover { border-color: var(--accent-strong); background: var(--accent-strong); }

/* The row menu. Destructive entry only, so it is one item, never a bar. */
.wh-menu {
  z-index: 100;
  min-width: 12rem;
  padding: 4px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 12px 30px color-mix(in srgb, #000 24%, transparent);
}
.wh-menu-item {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 2.75rem;
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}
.wh-menu-item:hover { background: var(--bg3); }
.wh-menu-item.danger { color: var(--error); }

/* The create form is two columns only where two fit; below that it is one, so
   no field is squeezed into a narrow strip. Keyed on the pane, not the window:
   the section is drawn in a split pane as narrow as ~380px on a wide screen. */
@container chat-pane (max-width: 560px) {
  .wh-form { grid-template-columns: minmax(0, 1fr); }
}
</style>