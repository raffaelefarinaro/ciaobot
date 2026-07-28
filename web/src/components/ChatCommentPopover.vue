<template>
  <Teleport to="body">
    <!-- Pinned popovers get a click-away backdrop; hover previews don't. -->
    <div
      v-if="popover?.pinned && comment"
      class="pop-backdrop"
      @click="close()"
    ></div>
    <div
      v-if="popover && comment"
      class="pop"
      :style="{ top: popover.top + 'px', left: popover.left + 'px' }"
      @mousedown.stop
      @mouseenter="onPopoverEnter"
      @mouseleave="onPopoverLeave"
    >
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
import { computed } from 'vue'
import { useHoverPinPopover } from '../composables/useHoverPinPopover'
import { clampAnchorLeft, clampAnchorTop } from '../lib/popoverAnchor'

type ChatComment = { id: string; comment: string; images?: string[] }

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
const POP_HEIGHT = 80

// Clamped to the viewport, since the popover is position: fixed.
function anchorFromElement(el: HTMLElement): { top: number; left: number } {
  const rect = el.getBoundingClientRect()
  return {
    top: clampAnchorTop(rect.bottom + 6, POP_HEIGHT),
    left: clampAnchorLeft(rect.left, POP_WIDTH),
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
} = useHoverPinPopover<ChatComment>({
  resolveTarget: highlightFromEvent,
  anchorFor: anchorFromElement,
  findComment: id => props.comments.find(c => c.id === id) ?? null,
  hasTargets: () => props.comments.length > 0,
})

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
  padding: 10px 12px;
  box-sizing: border-box;
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
