<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

export interface ModelSection {
  key: string
  label: string
  models: string[]
  badge?: string
  modelBadges?: Record<string, string[]>
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
  close: []
}>()

const open = ref(false)
const query = ref('')
const popoverRef = ref<HTMLElement | null>(null)
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
  }))
})

const normalizedQuery = computed(() => query.value.trim().toLowerCase())
const popoverVisible = computed(() => props.triggerless || open.value)

const filteredSections = computed<ModelSection[]>(() => {
  const q = normalizedQuery.value
  return normalizedSections.value
    .map((section) => {
      if (section.disabled) {
        return section
      }
      const models = q
        ? section.models.filter((m) => m.toLowerCase().includes(q))
        : section.models
      return { ...section, models }
    })
    .filter((section) => section.disabled || section.models.length > 0)
})

const hasAnyModels = computed(
  () => filteredSections.value.length > 0
)

const singleSelectedModel = computed(() => {
  if (props.multiple) return ''
  const v = effectiveValue.value as string
  return v
})

const multiSelectedModels = computed(() => {
  if (!props.multiple) return []
  return effectiveValue.value as string[]
})

const selectedCount = computed(() =>
  props.multiple ? (effectiveValue.value as string[]).length : effectiveValue.value ? 1 : 0
)

const triggerLabel = computed(() => {
  if (props.multiple) {
    const models = effectiveValue.value as string[]
    return models.length > 0 ? models.join(', ') : props.emptyPlaceholder
  }
  const v = effectiveValue.value as string
  return v || props.placeholder
})

const activeModelSet = computed(() => {
  const explicit = props.activeModels.map((model) => model.trim()).filter(Boolean)
  if (explicit.length) return new Set(explicit)
  return new Set(
    (props.multiple ? (effectiveValue.value as string[]) : [effectiveValue.value as string])
      .map((model) => model.trim())
      .filter(Boolean),
  )
})

function toggle() {
  if (props.disabled) return
  open.value ? close() : openPopover()
}

function openPopover() {
  open.value = true
  query.value = ''
  nextTick(() => {
    searchRef.value?.focus()
    scrollActiveIntoView()
  })
}

function close() {
  open.value = false
  query.value = ''
  if (props.triggerless) emit('close')
}

function selectModel(model: string) {
  if (props.multiple) {
    const current = new Set(effectiveValue.value as string[])
    if (current.has(model)) {
      current.delete(model)
    } else {
      current.add(model)
    }
    emit('update:modelValue', Array.from(current))
    nextTick(() => searchRef.value?.focus())
  } else {
    emit('update:modelValue', model)
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
  const aliases = normalizedSections.value
    .find((section) => section.models.includes(model))
    ?.modelBadges?.[model] || []
  return aliases.some((alias) => activeModelSet.value.has(alias.toLowerCase()))
}

function modelBadges(section: ModelSection, model: string): string[] {
  return section.modelBadges?.[model] || []
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
      selectModel(focused.dataset.model || '')
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

function onClickOutside(event: MouseEvent) {
  const target = event.target as Node
  if (
    open.value &&
    !popoverRef.value?.contains(target) &&
    !triggerRef.value?.contains(target)
  ) {
    close()
  }
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('mousedown', onClickOutside)
  } else {
    document.removeEventListener('mousedown', onClickOutside)
  }
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

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onClickOutside)
})
</script>

<template>
  <div
    class="model-selector"
    :class="{
      'model-selector--open': popoverVisible,
      'model-selector--disabled': disabled,
      'model-selector--triggerless': triggerless,
    }"
  >
    <button
      v-if="!triggerless"
      ref="triggerRef"
      type="button"
      class="model-selector__trigger"
      :disabled="disabled"
      :aria-expanded="popoverVisible"
      aria-haspopup="listbox"
      @click="toggle"
    >
      <span class="model-selector__label">{{ triggerLabel }}</span>
      <span class="model-selector__chevron" aria-hidden="true">▾</span>
    </button>

    <div
      v-if="popoverVisible"
      ref="popoverRef"
      class="model-selector__popover"
      :class="`model-selector__popover--${placement}`"
      role="listbox"
      :aria-multiselectable="multiple"
    >
      <div v-if="searchable" class="model-selector__search-wrap">
        <input
          ref="searchRef"
          v-model="query"
          type="text"
          class="model-selector__search"
          placeholder="Search models..."
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
            :disabled="section.disabled"
            role="option"
            :aria-selected="isActive(model)"
            @click="selectModel(model)"
          >
            <span v-if="multiple" class="model-selector__check" aria-hidden="true">
              <span v-if="isSelected(model)" class="model-selector__checkmark">✓</span>
            </span>
            <span class="model-selector__item-main">
              <span class="model-selector__item-label">{{ model }}</span>
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
  </div>
</template>

<style scoped>
.model-selector {
  position: relative;
  display: inline-flex;
  align-items: center;
  width: 100%;
}

.model-selector--triggerless {
  width: auto;
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
  position: absolute;
  z-index: 100;
  min-width: 320px;
  max-width: min(480px, calc(100vw - 24px));
  max-height: min(420px, calc(100vh - var(--safe-top) - var(--safe-bottom) - 48px));
  display: flex;
  flex-direction: column;
  background: var(--bg2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
}

.model-selector__popover--bottom-start {
  top: calc(100% + 4px);
  left: 0;
}

.model-selector__popover--bottom-end {
  top: calc(100% + 4px);
  right: 0;
}

.model-selector__popover--top-start {
  bottom: calc(100% + 4px);
  left: 0;
}

.model-selector__popover--top-end {
  bottom: calc(100% + 4px);
  right: 0;
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
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--fg2);
}

.model-selector__badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--accent2);
  color: white;
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
  background: var(--accent);
  color: white;
}

.ms-item--active:hover,
.ms-item--active:focus {
  background: var(--accent-strong);
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
  color: white;
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
  border-radius: 999px;
  font-size: 10px;
  line-height: 1.2;
  color: var(--fg);
  background: var(--bg3);
  border: 1px solid var(--border);
}

.model-selector__item-badge--tier {
  color: white;
  background: var(--accent2);
  border-color: transparent;
}

.model-selector__item-badge--local {
  color: var(--fg);
  background: var(--bg-elev);
}

.ms-item--active .model-selector__item-badge {
  color: white;
  background: rgba(255, 255, 255, 0.18);
  border-color: rgba(255, 255, 255, 0.28);
}

.model-selector__empty {
  padding: 16px;
  text-align: center;
  font-size: 13px;
  color: var(--fg2);
}
</style>
