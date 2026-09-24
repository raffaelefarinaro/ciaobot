<template>
  <Teleport to="body">
    <!-- Pinned popovers get a click-away backdrop; hover previews don't. -->
    <div
      v-if="popover?.pinned && comment"
      class="pop-backdrop"
      @click="onBackdropClick"
    ></div>
    <FocusScope
      v-if="popover && comment"
      as-child
      loop
      :trapped="false"
      @mount-auto-focus="onMountAutoFocus"
      @unmount-auto-focus="onUnmountAutoFocus"
    >
      <div
        ref="popEl"
        class="pop"
        role="dialog"
        aria-label="Comment"
        :style="{ top: popover.top + 'px', left: popover.left + 'px' }"
        @mousedown.stop
        @mouseenter="onPopoverEnter"
        @mouseleave="onPopoverLeave"
        @keydown="onKeydown"
      >
        <div class="pop-header">
          <div class="pop-actions">
            <button class="pop-btn-edit" @click.stop="onEdit" title="Edit">✎</button>
            <button class="pop-btn-remove" @click.stop="onDelete" title="Delete">×</button>
          </div>
        </div>
        <div v-if="comment.images?.length" class="pop-images">
          <img
            v-for="img in comment.images"
            :key="img"
            :src="`/api/images/${img}`"
            :alt="img"
            class="pop-thumb"
            @click.stop
          />
        </div>
        <div class="pop-note">{{ comment.comment }}</div>
      </div>
    </FocusScope>
  </Teleport>
</template>

<script setup lang="ts">
// Read popover for a commented span in the chat transcript.
//
// This owns its popover state rather than taking it as a prop, and that is the
// point. ChatPanel renders the whole non-virtualized transcript inline, so any
// popover state read in ChatPanel's template made every hover re-run its render
// function and re-evaluate traceSummaryMetaParts for every message: measured at
// 200 calls per hover-in and 200 more per hover-out on a 200-message chat.
// Keeping the state here confines a hover to this component's own render.
//
// The parent drives it imperatively through the exposed handlers, which it binds
// at event time rather than render time so the ref is not a render dependency.
import { computed, nextTick, onScopeDispose, ref, watch } from 'vue'
import { FocusScope } from 'reka-ui'
import { useHoverPinPopover } from '../composables/useHoverPinPopover'
import { useViewportHeight } from '../composables/useViewportHeight'
import { clampAnchorLeft, clampAnchorTop } from '../lib/popoverAnchor'
import { onViewportChange, viewportWidth } from '../lib/viewport'

type ChatComment = { id: string; comment: string; images?: string[] }

const emit = defineEmits<{
  (e: 'edit', comment: ChatComment): void
  (e: 'delete', id: string): void
}>()

const props = defineProps<{
  comments: ChatComment[]
  /** Id of the in-flight draft highlight, which has no saved comment to show. */
  draftId: string
}>()

function highlightFromEvent(e: MouseEvent): HTMLElement | null {
  const target = e.target as HTMLElement | null
  const highlight = target?.closest('.comment-highlight') as HTMLElement | null
  const id = highlight?.dataset.commentId
  return highlight && id && id !== props.draftId ? highlight : null
}

const POP_WIDTH = 280
const POP_FALLBACK_HEIGHT = 80
const measuredHeight = ref(POP_FALLBACK_HEIGHT)

// The box is position: fixed, so its containing block is the visual viewport,
// not the layout viewport. Keep both dimensions reactive: iOS can shrink the
// visible height for the software keyboard and rotation can narrow the width
// after the popover has already been opened.
const viewportH = useViewportHeight()
const viewportW = ref(viewportWidth())
const stopViewportChange = onViewportChange(() => {
  viewportW.value = viewportWidth()
})
onScopeDispose(stopViewportChange)

const popEl = ref<HTMLElement | null>(null)

// Clamped to the viewport, since the popover is position: fixed.
function anchorFromElement(el: HTMLElement): { top: number; left: number } {
  const rect = el.getBoundingClientRect()
  return {
    top: clampAnchorTop(rect.bottom + 6, measuredHeight.value, viewportH.value),
    left: clampAnchorLeft(rect.left, POP_WIDTH, viewportW.value),
  }
}

const {
  popover,
  comment,
  show,
  close,
  clearPendingClose,
  onTargetOver,
  onTargetOut,
  onPopoverEnter,
  onPopoverLeave,
  reposition,
} = useHoverPinPopover<ChatComment>({
  resolveTarget: highlightFromEvent,
  anchorFor: anchorFromElement,
  findComment: id => props.comments.find(c => c.id === id) ?? null,
  hasTargets: () => props.comments.length > 0,
})

let pinTimestamp = 0
function measureAndReposition(): void {
  nextTick(() => {
    const height = popEl.value?.offsetHeight
    if (height) measuredHeight.value = height
    reposition()
  })
}

watch(() => popover.value?.id, () => {
  measuredHeight.value = POP_FALLBACK_HEIGHT
  measureAndReposition()
})
watch(() => comment.value?.images?.length, measureAndReposition)

watch(() => popover.value?.pinned, (pinned) => {
  if (pinned) {
    pinTimestamp = Date.now()
    // A hover preview must never steal focus. Once the user has explicitly
    // pinned it, focus the first action so keyboard users can edit/delete
    // without tabbing through the transcript first.
    nextTick(() => popEl.value?.querySelector<HTMLElement>('button')?.focus())
  }
})

// Re-clamp an already-open popover when the software keyboard or orientation
// changes. `onViewportChange` is intentionally used instead of a scroll
// listener: iOS visualViewport scroll events fire while the caret is being
// kept visible and must not make this box jump.
watch([viewportH, viewportW], () => {
  if (popover.value) reposition()
})

let skipNextFocusRestore = false

function onMountAutoFocus(event: Event): void {
  // FocusScope is used as an app-specific bridge here, not as a modal: hover
  // previews are informational and must not move focus out of the transcript.
  // Explicit pins are focused by the watcher above.
  event.preventDefault()
}

function onUnmountAutoFocus(event: Event): void {
  // Edit/Delete immediately hand focus to the composer or the next action;
  // allowing the old scope's deferred restoration to run would steal it back.
  if (skipNextFocusRestore) {
    skipNextFocusRestore = false
    event.preventDefault()
  }
}

function closeForHandoff(): void {
  skipNextFocusRestore = true
  close()
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  event.preventDefault()
  event.stopPropagation()
  close()
}

function onBackdropClick(): void {
  if (Date.now() - pinTimestamp < 150) return
  close()
}

const openId = computed(() => popover.value?.id ?? null)

/**
 * Pin the popover for the highlight under a click and return its comment id, so
 * the caller can sync other surfaces (scrolling the sidebar to the matching
 * card). Returns null when the click wasn't on a commented highlight.
 */
function pinFromEvent(e: MouseEvent): string | null {
  const highlight = highlightFromEvent(e)
  const id = highlight?.dataset.commentId
  if (!highlight || !id) return null
  show(id, highlight, true)
  return id
}

function onEdit(): void {
  if (!comment.value) return
  const c = comment.value
  closeForHandoff()
  emit('edit', c)
}

function onDelete(): void {
  if (!comment.value) return
  const id = comment.value.id
  closeForHandoff()
  emit('delete', id)
}

defineExpose({ show, close, clearPendingClose, onTargetOver, onTargetOut, pinFromEvent, openId })
</script>

<style scoped>
.pop-backdrop {
  position: fixed;
  inset: 0;
  z-index: 40;
  background: rgba(0, 0, 0, 0.32);
}
.pop {
  position: fixed;
  z-index: 41;
  width: 280px;
  max-width: calc(100vw - 16px);
  background: var(--bg);
  border: 1px solid var(--border-strong);
  border-left: 3px solid var(--accent, #60a5fa);
  border-radius: 8px;
  box-shadow: 0 10px 28px rgba(0, 0, 0, 0.45);
  padding: 8px 12px 10px;
  box-sizing: border-box;
}
.pop-header {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  margin-bottom: 2px;
}
.pop-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}
.pop-btn-edit,
.pop-btn-remove {
  background: transparent;
  border: none;
  color: var(--fg2);
  font-size: 13px;
  line-height: 1;
  width: var(--touch);
  height: var(--touch);
  padding: 0;
  border-radius: 6px;
  cursor: pointer;
}
.pop-btn-edit:hover,
.pop-btn-remove:hover,
.pop-btn-edit:focus-visible,
.pop-btn-remove:focus-visible {
  background: var(--bg2);
  color: var(--fg);
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.pop-images {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
}
.pop-thumb {
  height: 36px;
  width: 36px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--border);
  background: var(--bg);
}
.pop-note {
  margin: 0;
  color: var(--fg);
  word-break: break-word;
}
</style>
