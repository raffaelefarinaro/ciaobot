<template>
  <!-- A compact select as a menu button: the button names the current choice
       (and its one-line meaning); the menu lists the choices as radio items
       (arrow keys, Home/End, typeahead, Escape returns focus to the button).
       Disabled is aria-disabled, not the disabled attribute, so focus can come
       back to the button after a pick that starts a save. -->
  <DropdownMenuRoot :open="open" @update:open="setOpen">
    <DropdownMenuTrigger as-child>
      <button
        type="button"
        class="menu-select"
        :class="triggerClass"
        :aria-disabled="disabled ? 'true' : undefined"
        :aria-labelledby="labelledBy ? `${labelledBy} ${valueId}` : undefined"
        :aria-describedby="describedBy || undefined"
      >
        <span :id="valueId" class="menu-select__value">
          <span class="menu-select__name">{{ current ? current.label : emptyLabel }}</span>
          <span v-if="current ? current.description : emptyDescription" class="menu-select__detail">{{ current ? current.description : emptyDescription }}</span>
        </span>
        <svg class="menu-select__chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
      </button>
    </DropdownMenuTrigger>
    <DropdownMenuPortal>
      <!-- as-child, with the box in this template: the scoped styles reach only
           elements rendered here, not the popper wrapper Reka builds. -->
      <DropdownMenuContent as-child side="top" align="start" :side-offset="4" :collision-padding="8">
        <!-- Marked so the model picker that holds this menu knows a press in
             it is not a press outside the picker. -->
        <div class="menu-select__menu" data-model-picker-menu>
          <DropdownMenuRadioGroup :model-value="current?.value ?? ''" @update:model-value="onPick">
            <DropdownMenuRadioItem
              v-for="option in options"
              :key="option.value"
              :value="option.value"
              :text-value="option.label"
              class="menu-select__option"
            >
              <span class="menu-select__tick" aria-hidden="true">
                <DropdownMenuItemIndicator>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                </DropdownMenuItemIndicator>
              </span>
              <span class="menu-select__option-text">
                <span class="menu-select__option-name">{{ option.label }}</span>
                <span v-if="option.description" class="menu-select__option-detail">{{ option.description }}</span>
              </span>
            </DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </div>
      </DropdownMenuContent>
    </DropdownMenuPortal>
  </DropdownMenuRoot>
</template>

<script setup lang="ts">
import { computed, ref, useId } from 'vue'
import {
  DropdownMenuContent,
  DropdownMenuItemIndicator,
  DropdownMenuPortal,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuRoot,
  DropdownMenuTrigger,
} from 'reka-ui'

export interface MenuSelectOption {
  value: string
  label: string
  description?: string
}

const props = withDefaults(defineProps<{
  modelValue?: string | null
  options: MenuSelectOption[]
  disabled?: boolean
  /** Id of the visible label that names the control. */
  labelledBy?: string
  describedBy?: string
  /** What the button reads when the value is none of the options. */
  emptyLabel?: string
  emptyDescription?: string
  triggerClass?: string
}>(), {
  modelValue: null,
  disabled: false,
  labelledBy: '',
  describedBy: '',
  emptyLabel: 'Unknown',
  emptyDescription: '',
  triggerClass: '',
})

const emit = defineEmits<{ select: [value: string] }>()

const open = ref(false)
const valueId = useId()
const current = computed(() => props.options.find(option => option.value === props.modelValue) ?? null)

function setOpen(next: boolean): void {
  open.value = next && !props.disabled
}

function onPick(value: unknown): void {
  if (props.disabled || typeof value !== 'string') return
  if (props.options.some(option => option.value === value)) emit('select', value)
}
</script>

<style scoped>
.menu-select {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-width: 0;
  /* The model rows' height, and the touch minimum on a coarse pointer. */
  min-height: 40px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  color: var(--fg);
  font: inherit;
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  transition: border-color 120ms var(--ease), background 120ms var(--ease);
}

.menu-select:hover:not([aria-disabled='true']) {
  border-color: var(--border-strong);
  background: var(--bg-elev);
}

.menu-select[data-state='open'] {
  border-color: var(--accent);
}

.menu-select:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.menu-select[aria-disabled='true'] {
  opacity: 0.6;
  cursor: not-allowed;
}

.menu-select__value {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
}

.menu-select__name {
  flex: 0 0 auto;
  font-weight: 600;
}

.menu-select__detail {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--fg2);
}

.menu-select__chevron {
  flex: 0 0 auto;
  color: var(--fg2);
  transition: transform 120ms var(--ease);
}

.menu-select[data-state='open'] .menu-select__chevron {
  transform: rotate(180deg);
}

.menu-select__menu {
  /* Above the model popover (300) it opens from. */
  z-index: 310;
  box-sizing: border-box;
  /* As wide as its button, never narrower than the descriptions need, and
     never wider than the viewport (a phone at 200% zoom is 195px wide). */
  min-width: min(max(var(--reka-popper-anchor-width, 200px), 200px), calc(100vw - 16px));
  max-width: min(380px, calc(100vw - 16px));
  max-height: var(--reka-popper-available-height, 60dvh);
  overflow-y: auto;
  padding: var(--space-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 16px 36px -12px rgb(0 0 0 / 60%);
}

.menu-select__option {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 44px;
  padding: 6px 10px 6px 6px;
  border-radius: var(--radius-sm);
  color: var(--fg);
  cursor: pointer;
  outline: none;
  user-select: none;
}

.menu-select__option[data-highlighted] {
  background: var(--bg3);
}

.menu-select__option:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.menu-select__tick {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 18px;
  color: var(--accent);
}

.menu-select__option-text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.menu-select__option-name {
  font-size: var(--text-base);
  font-weight: 600;
}

.menu-select__option-detail {
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.3;
}

@media (pointer: coarse) {
  .menu-select { min-height: var(--touch); }
}

@media (prefers-reduced-motion: reduce) {
  .menu-select,
  .menu-select__chevron { transition: none; }
}
</style>
