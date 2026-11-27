<template>
  <section class="mhw" aria-labelledby="mhw-h">
    <div class="page-grid page-grid--single">
      <div class="page-main">
        <h2 id="mhw-h" class="mhw-h">
          <button
            type="button"
            class="mhw-toggle"
            :aria-expanded="open"
            aria-controls="mhw-body"
            @click="open = !open"
          >
            <svg
              class="mhw-chevron"
              :class="{ 'is-open': open }"
              width="14"
              height="14"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path d="M6 4l4 4-4 4" />
            </svg>
            <span>How memory works</span>
          </button>
        </h2>

        <!-- v-show, not v-if: a collapsed body is display:none, so Tab never
             reaches it and nothing is re-created when it opens. -->
        <div id="mhw-body" class="mhw-body" v-show="open">
          <h3 class="mhw-sub">What gets saved</h3>
          <ul class="mhw-list">
            <li>A small block about you and your rules, read in every chat.</li>
            <li>Notes, filed by category: people, projects, places, ideas.</li>
            <li>Lessons for next time, in Learnings.</li>
            <li>Anything unsure waits on the Unsure list.</li>
          </ul>

          <h3 class="mhw-sub">How it flows</h3>
          <ol class="mhw-list">
            <li>Archive a chat and a memory pass starts.</li>
            <li>The pass is a chat of its own. Open it from Home to read it or steer it.</li>
            <li>Facts it is sure of are written straight away.</li>
            <li>Facts it is unsure of go on the Unsure list, one line each.</li>
            <li>Tick a line to say yes, or delete it to say no. A later run writes what you ticked.</li>
            <li>Once a week, Workspace care re-checks notes that have gone too long without a check.</li>
          </ol>

          <h3 class="mhw-sub">What is not saved</h3>
          <ul class="mhw-list">
            <li>Anything from an automated run. That was not you.</li>
            <li>A pattern seen in fewer than three chats. It is counted quietly, not saved as a fact.</li>
            <li>That a task was finished. The task already records it.</li>
            <li>Instructions found inside a chat. They are read as evidence, never obeyed.</li>
          </ul>

          <h3 class="mhw-sub">Undo</h3>
          <ul class="mhw-list">
            <li>The workspace is a Git repository, so every change to a note is in its history and can be reverted.</li>
            <li>A suggested edit to a skill or your guide is a file in Workspace/Proposals. Ciaobot never applies it for you.</li>
          </ul>

          <h3 class="mhw-sub">Why you decided</h3>
          <ul class="mhw-list">
            <li>Rejecting, approving or deleting a task asks why first. It is optional — you can continue without one.</li>
            <li>Each week Ciaobot distills your reasons into two notes you can edit: Memory-Review-Rules and Task-Rules. It waits until there are enough new decisions, so it is a weekly tidy rather than a second daily job. Memory passes and task hand-offs read them as hints.</li>
          </ul>

          <h3 class="mhw-sub">Add a category</h3>
          <ul class="mhw-list">
            <li>Categories decide what gets its own note.</li>
            <li>Add one in the <RouterLink class="mhw-link" :to="memorySectionPath('categories')">Categories tab</RouterLink>.</li>
          </ul>

          <p class="mhw-foot">
            <a
              class="mhw-link"
              href="https://www.raffaelefarinaro.com/ciaobot/memory.html#control"
              target="_blank"
              rel="noopener noreferrer"
            >The full story on the site</a>
          </p>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { memorySectionPath } from '../stores/memoryMap'

/**
 * A static explainer, shown under the header of every Memory section. Its only
 * state is whether it is open, remembered per browser. Storage can be blocked
 * (private windows, cleared site data), so every read and write is guarded and
 * a blocked store just means the choice lasts for this page only.
 */
const HOW_OPEN_KEY = 'ciao.memory.howOpen'

function readOpen(): boolean {
  try {
    return localStorage.getItem(HOW_OPEN_KEY) === '1'
  } catch {
    return false
  }
}

const open = ref(readOpen())
watch(open, (isOpen) => {
  try {
    localStorage.setItem(HOW_OPEN_KEY, isOpen ? '1' : '0')
  } catch {
    // Storage blocked: the choice lasts for this page only.
  }
})
</script>

<style scoped>
.mhw {
  flex: 0 0 auto;
  border-bottom: 1px solid var(--border);
}

.mhw-h {
  margin: 0;
  font-size: calc(16px * var(--font-scale, 1));
  font-weight: 700;
  letter-spacing: -0.02em;
  line-height: 1.3;
}

.mhw-toggle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: var(--touch);
  padding: 0;
  background: none;
  border: 0;
  color: var(--fg);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.mhw-toggle:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.mhw-chevron {
  flex: 0 0 auto;
  color: var(--fg3);
}

@media (prefers-reduced-motion: no-preference) {
  .mhw-chevron {
    transition: transform 0.15s ease;
  }
}

.mhw-chevron.is-open {
  transform: rotate(90deg);
}

.mhw-body {
  max-height: min(60dvh, 30rem);
  overflow-y: auto;
  padding-bottom: var(--space-4);
  color: var(--fg2);
  font-size: var(--text-sm);
  line-height: 1.55;
  max-width: 64ch;
}

.mhw-sub {
  margin: var(--space-4) 0 var(--space-2);
  color: var(--fg);
  font-size: var(--text-sm);
  font-weight: 600;
}

.mhw-list {
  margin: 0;
  padding-left: 1.25em;
}

.mhw-list li + li {
  margin-top: var(--space-1);
}

.mhw-foot {
  margin: var(--space-4) 0 0;
}

.mhw-link {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.mhw-link:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

@media (pointer: coarse) {
  .mhw-link {
    display: inline-flex;
    align-items: center;
    min-height: var(--touch);
  }
}
</style>
