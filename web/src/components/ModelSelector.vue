<script setup lang="ts">
import {
  PopoverAnchor,
  PopoverContent,
  PopoverRoot,
  PopoverTrigger,
} from 'reka-ui'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'

export interface ModelSection {
  key: string
  label: string
  models: string[]
  badge?: string
  modelBadges?: Record<string, string[]>
  modelLabels?: Record<string, string>
  disabled?: boolean
  hint?: string
}

interface Props {
  modelValue?: string | string[]
  sections: ModelSection[]
  multiple?: boolean
  placeholder?: string
  emptyPlaceholder?: string
  searchable?: boolean
  disabled?: boolean
  placement?: 'bottom-start' | 'bottom-end' | 'top-start' | 'top-end'
  activeModels?: string[]
  triggerless?: boolean
  mobileSheet?: boolean
  // When set, only the section with this key is shown. Used by the
  // image-capability card's "Open picker" to land the user on the current
  // backend instead of the full cross-provider list.
  filterSection?: string
  // Ids of a heading and a description rendered in the header slot, so the
  // listbox and its search field can name and explain the picker instead of
  // leaving that text visual-only. With a trigger, `labelledBy` is also the
  // id of the row label that names the picker (Settings "Default model"), so
  // a screen reader hears "Default model, Engine default (sonnet)" instead of
  // two look-alike buttons named only by their current value (#1924).
  labelledBy?: string
  describedBy?: string
}

const props = withDefaults(defineProps<Props>(), {
  modelValue: '',
  multiple: false,
  placeholder: 'Select model',
  emptyPlaceholder: 'None selected',
  searchable: true,
  disabled: false,
  placement: 'bottom-start',
  activeModels: () => [],
  triggerless: false,
  mobileSheet: false,
  labelledBy: '',
  describedBy: '',
})

const emit = defineEmits<{
  'update:modelValue': [value: string | string[]]
  select: [value: string | string[], sectionKey: string]
  close: [reason: 'escape' | 'other' | 'close']
}>()

const open = ref(false)
const query = ref('')
// The trigger's visible value, so its accessible name can be the row label
// followed by the current model rather than the label alone.
const triggerLabelId = useId()
const triggerLabelledBy = computed(() =>
  props.labelledBy ? `${props.labelledBy} ${triggerLabelId}` : undefined,
)
const rootRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)
let preserveFocusOnUnmount = false
onBeforeUnmount(() => {
  const active = document.activeElement
  preserveFocusOnUnmount = !!active && active !== document.body && !rootRef.value?.contains(active)
})

const effectiveValue = computed<string | string[]>(() => {
  if (props.multiple) return Array.isArray(props.modelValue) ? props.modelValue : []
  return typeof props.modelValue === 'string' ? props.modelValue : ''
})

const normalizedSections = computed<ModelSection[]>(() => {
  return props.sections.map((section) => ({
    ...section,
    models: section.models.map((m) => m.trim()).filter(Boolean),
    modelBadges: section.modelBadges || {},
    modelLabels: section.modelLabels || {},
  }))
})

const normalizedQuery = computed(() => query.value.trim().toLowerCase())
const popoverVisible = computed(() => props.triggerless || open.value)

const filteredSections = computed<ModelSection[]>(() => {
  const q = normalizedQuery.value
  return normalizedSections.value
    .filter((section) => !props.filterSection || section.key === props.filterSection)
    .map((section) => {
      if (section.disabled) {
        return section
      }
      const models = q
        ? section.models.filter((m) => (
          m.toLowerCase().includes(q)
          || (section.modelLabels?.[m] || '').toLowerCase().includes(q)
          || (section.modelBadges?.[m] || []).some((badge) => badge.toLowerCase().includes(q))
        ))
        : section.models
      return { ...section, models }
    })
    .filter((section) => section.disabled || section.models.length > 0)
})

const hasAnyModels = computed(
  () => filteredSections.value.length > 0
)

function displayModelLabel(model: string): string {
  const section = normalizedSections.value.find((item) => item.models.includes(model))
  return section?.modelLabels?.[model] || model
}

const triggerLabel = computed(() => {
  if (props.multiple) {
    const models = effectiveValue.value as string[]
    return models.length > 0 ? models.map(displayModelLabel).join(', ') : props.emptyPlaceholder
  }
  const v = effectiveValue.value as string
  return v ? displayModelLabel(v) : props.placeholder
})

const explicitActiveModels = computed(() =>
  props.activeModels.map((model) => model.trim()).filter(Boolean),
)

const activeModelSet = computed(() => {
  if (explicitActiveModels.value.length) return new Set(explicitActiveModels.value)
  return new Set(
    (props.multiple ? (effectiveValue.value as string[]) : [effectiveValue.value as string])
      .map((model) => model.trim())
      .filter(Boolean),
  )
})

function onPopoverOpenChange(nextOpen: boolean): void {
  if (props.disabled && nextOpen) return
  if (nextOpen) {
    open.value = true
    query.value = ''
    nextTick(() => {
      searchRef.value?.focus()
      scrollActiveIntoView()
    })
    return
  }
  close('other')
}

function close(reason: 'escape' | 'other' | 'close' = 'other') {
  const returnFocus = !props.triggerless ? triggerRef.value : null
  const active = document.activeElement
  const shouldReturnFocus = !active || active === document.body || !!rootRef.value?.contains(active)
  open.value = false
  query.value = ''
  if (returnFocus && shouldReturnFocus) nextTick(() => {
    const current = document.activeElement
    if (returnFocus.isConnected && (!current || current === document.body || rootRef.value?.contains(current))) {
      returnFocus.focus()
    }
  })
  if (props.triggerless) emit('close', reason)
}

function selectModel(model: string, sectionKey: string) {
  if (props.multiple) {
    const current = new Set(effectiveValue.value as string[])
    if (current.has(model)) {
      current.delete(model)
    } else {
      current.add(model)
    }
    const selected = Array.from(current)
    emit('update:modelValue', selected)
    emit('select', selected, sectionKey)
    nextTick(() => searchRef.value?.focus())
  } else {
    emit('update:modelValue', model)
    emit('select', model, sectionKey)
    close('other')
  }
}

function isSelected(model: string): boolean {
  if (props.multiple) {
    return (effectiveValue.value as string[]).includes(model)
  }
  return effectiveValue.value === model
}

function isActive(model: string): boolean {
  if (activeModelSet.value.has(model)) return true
  // Tier-alias fallback: a bare tier value (e.g. "opus") highlights the
  // provider-native model carrying that tier badge. Only applies when no
  // explicit activeModels were given — those are already the exact models in
  // use (resolved per provider), so alias matching would wrongly light up
  // same-tier models from other providers.
  if (explicitActiveModels.value.length) return false
  const aliases = normalizedSections.value
    .find((section) => section.models.includes(model))
    ?.modelBadges?.[model] || []
  return aliases.some((alias) => activeModelSet.value.has(alias.toLowerCase()))
}

function modelBadges(section: ModelSection, model: string): string[] {
  return section.modelBadges?.[model] || []
}

function modelLabel(section: ModelSection, model: string): string {
  return section.modelLabels?.[model] || model
}

function badgeClass(badge: string): string {
  return badge.toLowerCase() === 'local'
    ? 'model-selector__item-badge--local'
    : 'model-selector__item-badge--tier'
}

function scrollActiveIntoView() {
  if (!listRef.value) return
  const active = listRef.value.querySelector('.ms-item--active') as HTMLElement | null
  if (active && typeof active.scrollIntoView === 'function') {
    active.scrollIntoView({ block: 'nearest' })
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) {
    if (event.key === 'Escape') event.stopPropagation()
    return
  }
  if (event.key === 'Escape') {
    // Claim the key. Dismissing this popover is the whole meaning of the press,
    // and the global handler in ChatLayout treats an unclaimed Escape as "go
    // home" - so without this, closing the picker also navigated away, out of
    // half-finished Settings edits.
    event.preventDefault()
    event.stopPropagation()
    close('escape')
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    focusAdjacentItem(event.key === 'ArrowDown' ? 1 : -1)
    return
  }
  const onItem = (document.activeElement as HTMLElement | null)?.classList.contains('ms-item')
  if (onItem && (event.key === 'Home' || event.key === 'End')) {
    event.preventDefault()
    focusEdgeItem(event.key === 'Home' ? 'first' : 'last')
    return
  }
  // Typing on a highlighted row keeps filtering: the character goes to the
  // search field, which takes focus back, as in a combobox.
  if (onItem && props.searchable && event.key.length === 1 && event.key !== ' '
    && !event.metaKey && !event.ctrlKey && !event.altKey) {
    event.preventDefault()
    query.value += event.key
    searchRef.value?.focus()
    return
  }
  if (onItem && props.searchable && event.key === 'Backspace') {
    event.preventDefault()
    query.value = query.value.slice(0, -1)
    searchRef.value?.focus()
    return
  }
  if (event.key === 'Enter' || event.key === ' ') {
    const focused = document.activeElement as HTMLElement | null
    if (focused) {
      if (!focused.classList.contains('ms-item')) return
      event.preventDefault()
      selectModel(focused.dataset.model || '', focused.dataset.section || '')
    }
  }
}

function enabledItems(): HTMLElement[] {
  return (Array.from(listRef.value?.querySelectorAll('.ms-item') || []) as HTMLButtonElement[])
    .filter((el) => !el.disabled)
}

function focusEdgeItem(edge: 'first' | 'last') {
  const items = enabledItems()
  const target = edge === 'first' ? items[0] : items[items.length - 1]
  target?.focus()
}

function focusAdjacentItem(direction: 1 | -1) {
  const items = enabledItems()
  if (!items.length) return
  const activeEl = document.activeElement as HTMLElement
  const currentIndex = items.findIndex((el) => el === activeEl)
  let nextIndex = currentIndex + direction
  if (nextIndex < 0) nextIndex = items.length - 1
  if (nextIndex >= items.length) nextIndex = 0
  items[nextIndex]?.focus()
}

// Reka's Popper owns the fixed-position collision work. Keeping these as
// explicit props (rather than writing top/left after mount) means the same
// placement is used for the first paint, scroll updates, visual-viewport
// resizes, and filtered-list height changes.
const POPOVER_GAP = 4
const POPOVER_MARGIN = 8
const popoverSide = computed(() => props.placement.startsWith('top') ? 'top' : 'bottom')
const popoverAlign = computed(() => props.placement.endsWith('-end') ? 'end' : 'start')

function focusSearch(): void {
  nextTick(() => {
    searchRef.value?.focus()
    scrollActiveIntoView()
  })
}

function onOpenAutoFocus(event: Event): void {
  // The selector is a searchable listbox, so focus the query field rather than
  // letting Reka's generic dialog autofocus land on its container. Preventing
  // the default also keeps the caret out of the search field on mouse opening
  // when the caller has explicitly asked for pointer focus.
  event.preventDefault()
  focusSearch()
}

function onCloseAutoFocus(event: Event): void {
  // Never override focus that has already moved outside the picker.
  const active = document.activeElement
  if (preserveFocusOnUnmount || (active && active !== document.body && !rootRef.value?.contains(active))) {
    event.preventDefault()
  }
}

function onEscapeKeyDown(event: KeyboardEvent): void {
  if (event.isComposing || event.keyCode === 229) {
    // Reka dismisses its layer unless this window-level event is prevented.
    event.preventDefault()
    return
  }
  // Claim Escape locally. ChatLayout treats an unclaimed Escape as navigation
  // away from the current pane.
  event.preventDefault()
  event.stopPropagation()
  close('escape')
}

watch(popoverVisible, (visible) => {
  if (visible) focusSearch()
})

// Search edits retain focus; prop/section changes must not steal footer focus.
watch(query, () => {
  if (popoverVisible.value) {
    nextTick(() => {
      searchRef.value?.focus()
      scrollActiveIntoView()
    })
  }
})

onMounted(() => {
  if (props.triggerless) {
    nextTick(() => {
      searchRef.value?.focus()
      scrollActiveIntoView()
    })
  }
})
</script>

<template>
  <PopoverRoot
    :open="popoverVisible"
    :modal="false"
    @update:open="onPopoverOpenChange"
  >
    <PopoverAnchor as-child>
      <div
        ref="rootRef"
        class="model-selector"
        :class="{
          'model-selector--open': popoverVisible,
          'model-selector--disabled': disabled,
          'model-selector--triggerless': triggerless,
          'model-selector--mobile-sheet': mobileSheet,
        }"
      >
        <PopoverTrigger v-if="!triggerless" as-child :disabled="disabled">
        <button
          ref="triggerRef"
          type="button"
          class="model-selector__trigger"
          :disabled="disabled"
          :title="triggerLabel"
          :aria-expanded="popoverVisible"
          :aria-labelledby="triggerLabelledBy"
          aria-haspopup="listbox"
        >
          <span :id="triggerLabelId" class="model-selector__label">{{ triggerLabel }}</span>
          <svg class="model-selector__chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
        </button>
      </PopoverTrigger>

      <PopoverContent
        v-if="popoverVisible"
        as-child
        :side="popoverSide"
        :align="popoverAlign"
        :side-offset="POPOVER_GAP"
        :collision-padding="POPOVER_MARGIN"
        :position-strategy="'fixed'"
        :update-position-strategy="'always'"
        @open-auto-focus="onOpenAutoFocus"
        @close-auto-focus="onCloseAutoFocus"
        @escape-key-down="onEscapeKeyDown"
      >
        <div
          class="model-selector__popover"
          :class="[`model-selector__popover--${placement}`, 'model-selector__popover--placed']"
          aria-label="Model selector"
        >
          <div v-if="mobileSheet" class="model-selector__mobile-header">
            <span>Choose model</span>
            <button
              type="button"
              class="model-selector__close"
              aria-label="Close model picker"
              @click.stop="close('close')"
            ><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12" /></svg></button>
          </div>
          <div
            class="model-selector__listbox"
          role="listbox"
          :aria-multiselectable="multiple"
          :aria-labelledby="labelledBy || undefined"
        >
          <div v-if="$slots.header" class="model-selector__header">
            <slot name="header" />
          </div>

          <div v-if="searchable" class="model-selector__search-wrap">
            <input
              ref="searchRef"
              v-model="query"
              type="text"
              class="model-selector__search"
              placeholder="Search models"
              :aria-describedby="describedBy || undefined"
              @keydown="onKeydown"
            />
          </div>

          <div
            v-if="hasAnyModels"
            ref="listRef"
            class="model-selector__list"
            tabindex="-1"
            @keydown="onKeydown"
          >
            <div
              v-for="section in filteredSections"
              :key="section.key"
              class="model-selector__section"
              :class="{ 'model-selector__section--disabled': section.disabled }"
            >
              <div class="model-selector__section-header">
                <span class="model-selector__section-label">{{ section.label }}</span>
                <span v-if="section.badge" class="model-selector__badge">{{ section.badge }}</span>
              </div>
              <p v-if="section.hint" class="model-selector__hint">{{ section.hint }}</p>
              <button
                v-for="model in section.models"
                :key="`${section.key}-${model}`"
                type="button"
                class="model-selector__item ms-item"
                :class="{
                  'ms-item--active': isActive(model),
                }"
                :data-model="model"
                :data-section="section.key"
                :title="model"
                :disabled="section.disabled"
                role="option"
                :aria-selected="isActive(model)"
                @click="selectModel(model, section.key)"
              >
                <span v-if="multiple" class="model-selector__check" aria-hidden="true">
                  <svg v-if="isSelected(model)" class="model-selector__checkmark" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                </span>
                <!-- Single select marks the current model with a check on the
                     hover surface, not a filled accent row that reads as a button. -->
                <span v-else class="model-selector__tick" aria-hidden="true"><svg v-if="isActive(model)" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg></span>
                <span class="model-selector__item-main">
                  <span class="model-selector__item-label">{{ modelLabel(section, model) }}</span>
                  <span v-if="modelBadges(section, model).length" class="model-selector__item-badges">
                    <span
                      v-for="badge in modelBadges(section, model)"
                      :key="`${section.key}-${model}-${badge}`"
                      class="model-selector__item-badge"
                      :class="badgeClass(badge)"
                    >{{ badge }}</span>
                  </span>
                </span>
              </button>
            </div>
          </div>

          <div v-else class="model-selector__empty">
            No models match "{{ query }}"
          </div>

          </div>
          <div v-if="$slots.footer" class="model-selector__footer">
            <slot name="footer" />
          </div>
        </div>
      </PopoverContent>
      </div>
    </PopoverAnchor>
  </PopoverRoot>
</template>

<style scoped>
.model-selector {
  position: relative;
  display: inline-flex;
  align-items: center;
  width: 100%;
}

.model-selector--triggerless {
  width: 100%;
  height: 100%;
  min-height: 30px;
  align-self: stretch;
}

.model-selector__trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  min-height: 36px;
  padding: 7px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elev);
  color: var(--fg);
  font: inherit;
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  transition: background 120ms var(--ease), border-color 120ms var(--ease);
}

.model-selector__trigger:hover:not(:disabled) {
  background: var(--bg3);
}

.model-selector__trigger:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.model-selector--open .model-selector__trigger {
  border-color: var(--accent);
}

.model-selector__label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.model-selector__chevron {
  flex: 0 0 auto;
  color: var(--fg2);
  transition: transform 120ms var(--ease);
}

.model-selector--open .model-selector__chevron {
  transform: rotate(180deg);
}

.model-selector__popover {
  /* Popper supplies the fixed placement and collision transform. The
     component's own styles only describe the elevated surface. */
  z-index: 300;
  display: flex;
  flex-direction: column;
  min-height: 0;
  /* The model list is the task: a fixed, roomy width (long opencode ids fit)
     and up to 640px of height where the viewport has it. */
  width: min(520px, calc(100vw - 24px));
  min-width: min(320px, calc(100vw - 24px));
  max-width: calc(100vw - 24px);
  /* Reka reports the room left on the chosen side (flipped above the trigger
     on short landscape phones); the viewport bound alone can overflow it. */
  max-height: min(
    640px,
    calc(100dvh - var(--safe-top) - var(--safe-bottom) - 32px),
    calc(var(--reka-popper-available-height, 100dvh) - 8px)
  );
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
  overflow: hidden;
}

.model-selector__listbox {
  display: flex;
  flex-direction: column;
  min-height: 0;
  flex: 1 1 auto;
  overflow: visible;
}

.model-selector__search-wrap {
  padding: 8px;
  border-bottom: 1px solid var(--border);
  flex: 0 0 auto;
}

.model-selector__search {
  width: 100%;
  box-sizing: border-box;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-elev);
  color: var(--fg);
  font: inherit;
  font-size: 14px;
}

.model-selector__search:focus {
  outline: none;
  border-color: var(--accent);
}

.model-selector__list {
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 0 6px 6px;
  flex: 1 1 auto;
  min-height: 0;
  scroll-padding-top: 36px;
  scrollbar-width: thin;
  scrollbar-color: var(--border-strong) transparent;
}

.model-selector__section + .model-selector__section {
  margin-top: 4px;
  border-top: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
}

.model-selector__section--disabled {
  opacity: 0.55;
}

/* Provider headers stay pinned while their models scroll under them. */
.model-selector__section-header {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 8px 8px 4px;
  background: var(--bg2);
}

.model-selector__section-label {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--fg3);
}

.model-selector__badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent2) 22%, transparent);
  color: var(--fg);
}

.model-selector__hint {
  margin: 0 6px 6px;
  font-size: 11px;
  color: var(--fg2);
  line-height: 1.35;
}

.model-selector__item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 40px;
  padding: 8px 10px 8px 8px;
  border: none;
  border-radius: calc(var(--radius) - 2px);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: var(--text-base);
  text-align: left;
  cursor: pointer;
  transition: background 120ms var(--ease);
}

.model-selector__item:disabled {
  cursor: not-allowed;
}

.model-selector__item:hover:not(:disabled),
.model-selector__item:focus {
  background: var(--bg3);
  outline: none;
}

.model-selector__item:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

/* Must stay below the base .model-selector__* rules it overrides. */
@media (pointer: coarse) {
  .model-selector__item { min-height: var(--touch, 44px); }
  .model-selector__search { min-height: var(--touch, 44px); }
  .model-selector__trigger { min-height: var(--touch, 44px); }
}

.ms-item--active {
  background: var(--bg3);
  font-weight: 600;
}

.ms-item--active:hover,
.ms-item--active:focus {
  background: color-mix(in srgb, var(--bg3) 80%, var(--fg) 8%);
}

.model-selector__tick {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  flex: 0 0 auto;
  color: var(--accent);
}

.model-selector__check {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  flex: 0 0 auto;
  border: 1px solid var(--border-strong);
  border-radius: 4px;
  background: var(--bg-elev);
}

.ms-item--active .model-selector__check {
  border-color: var(--accent);
  background: var(--accent);
}

.model-selector__checkmark {
  color: var(--on-accent);
}

.model-selector__item-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}

.model-selector__item-main {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  width: 100%;
}

.model-selector__item-badges {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex: 0 0 auto;
}

.model-selector__item-badge {
  display: inline-flex;
  align-items: center;
  min-height: 16px;
  padding: 1px 5px;
  border-radius: var(--radius-xs, 4px);
  font-size: 10px;
  line-height: 1.2;
  color: var(--fg);
  background: var(--bg3);
  border: 1px solid var(--border);
}

.model-selector__item-badge--tier {
  color: var(--fg);
  background: color-mix(in srgb, var(--accent2) 22%, transparent);
  border-color: color-mix(in srgb, var(--accent2) 45%, var(--border));
}

.model-selector__item-badge--local {
  color: var(--fg);
  background: var(--bg-elev);
}



.model-selector__empty {
  padding: 16px;
  text-align: center;
  font-size: 13px;
  color: var(--fg2);
}

.model-selector__footer {
  flex: 0 0 auto;
  min-height: 0;
  overflow-y: auto;
  padding: 10px 12px;
  /* A control scrolled into view keeps clear of the footer's edge. */
  scroll-padding: 8px;
  border-top: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  background: var(--bg2);
  border-radius: 0 0 var(--radius) var(--radius);
}

@media (max-height: 600px) {
  .model-selector__footer {
    flex: 0 1 auto;
    scrollbar-gutter: stable;
    scrollbar-width: thin;
    scrollbar-color: var(--fg2) var(--bg3);
  }

  .model-selector__footer::-webkit-scrollbar {
    width: 10px;
  }

  .model-selector__footer::-webkit-scrollbar-track {
    background: var(--bg3);
    border-radius: 999px;
  }

  .model-selector__footer::-webkit-scrollbar-thumb {
    background: var(--fg2);
    border: 2px solid var(--bg3);
    border-radius: 999px;
  }
}

.model-selector__header {
  flex: 0 0 auto;
  padding: 8px 10px;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  background: var(--bg2);
  border-radius: var(--radius) var(--radius) 0 0;
}

.model-selector__mobile-header {
  display: none;
}

@media (prefers-reduced-motion: reduce) {
  .model-selector__trigger,
  .model-selector__chevron,
  .model-selector__item { transition: none; }
}

@media (max-width: 768px) {
  /* Header selectors have no trigger box of their own, so anchor their menu
     to the viewport below the shared top bar and keep both edges visible. */
  .model-selector--triggerless :deep([data-reka-popper-content-wrapper]) {
    /* The Popper wrapper is the positioning bridge. Turn it into a full-width
       layout box on a phone, then let the sheet child use absolute coordinates
       below the shared top bar. */
    position: fixed !important;
    top: 0 !important;
    bottom: auto !important;
    right: calc(12px + var(--safe-right)) !important;
    left: calc(12px + var(--safe-left)) !important;
    width: auto !important;
    min-width: 0 !important;
    max-width: none !important;
    transform: none !important;
  }
  .model-selector--mobile-sheet :deep([data-reka-popper-content-wrapper]) {
    bottom: 0 !important;
    height: auto !important;
    pointer-events: none;
  }
  .model-selector--mobile-sheet :deep([data-reka-popper-content-wrapper])::before {
    position: fixed;
    z-index: 0;
    inset: 0 calc(-12px - var(--safe-right)) 0 calc(-12px - var(--safe-left));
    background: color-mix(in srgb, var(--bg) 66%, transparent);
    content: '';
    pointer-events: auto;
  }
  .model-selector--mobile-sheet .model-selector__popover {
    position: absolute !important;
    z-index: 1;
    top: calc(61px + var(--safe-top) + 4px);
    right: 0;
    bottom: auto;
    left: 0;
    width: auto;
    min-width: 0;
    max-width: none;
    max-height: calc(var(--app-h, 100dvh) - 61px - var(--safe-top) - var(--safe-bottom) - 24px);
    pointer-events: auto;
  }
  .model-selector--mobile-sheet .model-selector__mobile-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex: 0 0 auto;
    min-height: 48px;
    padding: 0 4px 0 10px;
    border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
    color: var(--fg);
    font-size: var(--text-base);
    font-weight: 600;
  }
  .model-selector__close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: var(--touch, 44px);
    min-width: var(--touch, 44px);
    height: var(--touch, 44px);
    min-height: var(--touch, 44px);
    border: 0;
    border-radius: 999px;
    background: transparent;
    color: var(--fg2);
    font: inherit;
    line-height: 1;
    cursor: pointer;
    touch-action: manipulation;
  }
  .model-selector__close:hover,
  .model-selector__close:focus-visible {
    background: var(--bg3);
    color: var(--fg);
  }
  .model-selector__close:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
  }
}
</style>
