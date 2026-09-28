<script setup lang="ts">
import {
  PopoverAnchor,
  PopoverContent,
  PopoverRoot,
  PopoverTrigger,
} from 'reka-ui'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

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
  // When set, only the section with this key is shown. Used by the
  // image-capability card's "Open picker" to land the user on the current
  // backend instead of the full cross-provider list.
  filterSection?: string
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
})

const emit = defineEmits<{
  'update:modelValue': [value: string | string[]]
  select: [value: string | string[], sectionKey: string]
  close: []
}>()

const open = ref(false)
const query = ref('')
const rootRef = ref<HTMLElement | null>(null)
const triggerRef = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)

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
  close()
}

function close() {
  const returnFocus = !props.triggerless ? triggerRef.value : null
  open.value = false
  query.value = ''
  if (returnFocus) nextTick(() => returnFocus.focus())
  if (props.triggerless) emit('close')
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
    close()
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
  if (event.key === 'Escape') {
    // Claim the key. Dismissing this popover is the whole meaning of the press,
    // and the global handler in ChatLayout treats an unclaimed Escape as "go
    // home" - so without this, closing the picker also navigated away, out of
    // half-finished Settings edits.
    event.preventDefault()
    event.stopPropagation()
    close()
    triggerRef.value?.focus()
    return
  }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault()
    focusAdjacentItem(event.key === 'ArrowDown' ? 1 : -1)
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

function focusAdjacentItem(direction: 1 | -1) {
  const items = Array.from(listRef.value?.querySelectorAll('.ms-item') || []) as HTMLElement[]
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

function onEscapeKeyDown(event: KeyboardEvent): void {
  // Claim Escape locally. ChatLayout treats an unclaimed Escape as navigation
  // away from the current pane.
  event.preventDefault()
  event.stopPropagation()
  close()
}

watch(popoverVisible, (visible) => {
  if (visible) focusSearch()
})

// Auto-focus the search input and scroll active item into view whenever the
// filtered list changes while open.
watch(filteredSections, () => {
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
          aria-haspopup="listbox"
        >
          <span class="model-selector__label">{{ triggerLabel }}</span>
          <span class="model-selector__chevron" aria-hidden="true">▾</span>
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
        @escape-key-down="onEscapeKeyDown"
      >
        <div
          class="model-selector__popover"
          :class="[`model-selector__popover--${placement}`, 'model-selector__popover--placed']"
          aria-label="Model selector"
        >
          <div
            class="model-selector__listbox"
          role="listbox"
          :aria-multiselectable="multiple"
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
                :disabled="section.disabled"
                role="option"
                :aria-selected="isActive(model)"
                @click="selectModel(model, section.key)"
              >
                <span v-if="multiple" class="model-selector__check" aria-hidden="true">
                  <span v-if="isSelected(model)" class="model-selector__checkmark">✓</span>
                </span>
                <!-- Single select marks the current model with a check on the
                     hover surface, not a filled accent row that reads as a button. -->
                <span v-else class="model-selector__tick" aria-hidden="true">{{ isActive(model) ? '✓' : '' }}</span>
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

          <div v-if="$slots.footer" class="model-selector__footer">
            <slot name="footer" />
          </div>
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
  font-size: 12px;
  transition: transform 120ms var(--ease);
}

.model-selector--open .model-selector__chevron {
  transform: rotate(180deg);
}

.model-selector__popover {
  /* Popper supplies the fixed placement and collision transform. The
     component's own styles only describe the elevated surface. */
  z-index: 300;
  min-width: 320px;
  max-width: min(480px, calc(100vw - 24px));
  max-height: min(420px, calc(100dvh - var(--safe-top) - var(--safe-bottom) - 32px));
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}

.model-selector__listbox {
  display: flex;
  flex-direction: column;
  min-height: 0;
  max-height: inherit;
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
  padding: 6px;
  flex: 1 1 auto;
}

.model-selector__section {
  margin-bottom: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
}

.model-selector__section:last-child {
  margin-bottom: 0;
  padding-bottom: 0;
  border-bottom: none;
}

.model-selector__section--disabled {
  opacity: 0.55;
}

.model-selector__section-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  margin-bottom: 2px;
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

@media (pointer: coarse) {
  .model-selector__item { min-height: var(--touch, 44px); }
}

.model-selector__item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  min-height: 34px;
  padding: 7px 8px;
  border: none;
  border-radius: calc(var(--radius) - 2px);
  background: transparent;
  color: var(--fg);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
  transition: background 120ms var(--ease);
}

.model-selector__item:disabled {
  cursor: not-allowed;
}

.model-selector__item:hover,
.model-selector__item:focus {
  background: var(--bg3);
  outline: none;
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
  width: 16px;
  flex: 0 0 auto;
  color: var(--accent);
  font-size: 12px;
  text-align: center;
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
  font-size: 11px;
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
  padding: 8px 10px;
  border-top: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  background: var(--bg2);
  border-radius: 0 0 var(--radius) var(--radius);
}

.model-selector__header {
  flex: 0 0 auto;
  padding: 8px 10px;
  border-bottom: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  background: var(--bg2);
  border-radius: var(--radius) var(--radius) 0 0;
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
    right: calc(12px + var(--safe-right)) !important;
    bottom: auto !important;
    left: calc(12px + var(--safe-left)) !important;
    width: auto !important;
    min-width: 0 !important;
    max-width: none !important;
    transform: none !important;
  }
  .model-selector--triggerless .model-selector__popover {
    position: absolute !important;
    top: calc(61px + var(--safe-top) + 4px);
    right: 0;
    bottom: auto;
    left: 0;
    width: auto;
    min-width: 0;
    max-width: none;
    max-height: calc(100dvh - 61px - var(--safe-top) - var(--safe-bottom) - 20px);
  }
}
</style>
