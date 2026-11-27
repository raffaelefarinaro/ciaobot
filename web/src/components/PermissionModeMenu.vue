<template>
  <!-- The chat's permission mode as a menu button: the button names the current
       mode and what it allows; the menu lists the four modes as radio items
       (arrow keys, Home/End, typeahead, Escape returns focus to the button). -->
  <DropdownMenuRoot v-model:open="open">
    <DropdownMenuTrigger as-child :disabled="disabled">
      <button
        type="button"
        class="permission-mode-trigger"
        :disabled="disabled"
        :aria-labelledby="labelledBy ? `${labelledBy} ${valueId}` : undefined"
        :aria-describedby="describedBy || undefined"
      >
        <span :id="valueId" class="permission-mode-trigger__value">
          <span class="permission-mode-trigger__name">{{ current ? PERMISSION_MODE_COPY[current].name : 'Unknown' }}</span>
          <span class="permission-mode-trigger__allows">{{ current ? PERMISSION_MODE_COPY[current].allows : 'choose a mode' }}</span>
        </span>
        <svg class="permission-mode-trigger__chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m7 9 5 5 5-5" /></svg>
      </button>
    </DropdownMenuTrigger>
    <DropdownMenuPortal>
      <!-- as-child, with the box in this template: the scoped styles reach only
           elements rendered here, not the popper wrapper Reka builds. -->
      <DropdownMenuContent
        as-child
        side="top"
        align="start"
        :side-offset="4"
        :collision-padding="8"
      >
        <div class="permission-mode-menu">
          <DropdownMenuRadioGroup :model-value="current ?? ''" @update:model-value="onPick">
            <DropdownMenuRadioItem
              v-for="mode in modes"
              :key="mode"
              :value="mode"
              :text-value="PERMISSION_MODE_COPY[mode].name"
              class="permission-mode-option"
            >
              <span class="permission-mode-option__tick" aria-hidden="true">
                <DropdownMenuItemIndicator>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5" /></svg>
                </DropdownMenuItemIndicator>
              </span>
              <span class="permission-mode-option__text">
                <span class="permission-mode-option__name">{{ PERMISSION_MODE_COPY[mode].name }}</span>
                <span class="permission-mode-option__allows">{{ PERMISSION_MODE_COPY[mode].allows }}</span>
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
import { isBridgeMode, PERMISSION_MODE_COPY, type BridgeMode } from '../lib/permissionModes'

const props = withDefaults(defineProps<{
  /** The chat's saved mode; anything that is not a known mode reads as Unknown. */
  modelValue?: string | null
  modes: BridgeMode[]
  disabled?: boolean
  /** Id of the visible label that names the control ("Permission mode"). */
  labelledBy?: string
  describedBy?: string
}>(), { modelValue: null, disabled: false, labelledBy: '', describedBy: '' })

const emit = defineEmits<{ select: [mode: BridgeMode] }>()

const open = ref(false)
const valueId = useId()
const current = computed<BridgeMode | null>(() => (isBridgeMode(props.modelValue) ? props.modelValue : null))

function onPick(value: unknown): void {
  if (isBridgeMode(value)) emit('select', value)
}
</script>

<style scoped>
.permission-mode-trigger {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-width: 0;
  min-height: 36px;
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

.permission-mode-trigger:hover:not(:disabled) {
  border-color: var(--border-strong);
  background: var(--bg-elev);
}

.permission-mode-trigger[data-state='open'] {
  border-color: var(--accent);
}

.permission-mode-trigger:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.permission-mode-trigger:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.permission-mode-trigger__value {
  display: flex;
  align-items: baseline;
  gap: 6px;
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  white-space: nowrap;
}

.permission-mode-trigger__name {
  flex: 0 0 auto;
  font-weight: 600;
}

.permission-mode-trigger__allows {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--fg2);
}

.permission-mode-trigger__chevron {
  flex: 0 0 auto;
  color: var(--fg2);
  transition: transform 120ms var(--ease);
}

.permission-mode-trigger[data-state='open'] .permission-mode-trigger__chevron {
  transform: rotate(180deg);
}

.permission-mode-menu {
  /* Above the model popover (300) it opens from. */
  z-index: 310;
  box-sizing: border-box;
  min-width: max(var(--reka-dropdown-menu-trigger-width, 240px), 240px);
  max-width: min(380px, calc(100vw - 16px));
  max-height: var(--reka-dropdown-menu-content-available-height, 60dvh);
  overflow-y: auto;
  padding: var(--space-1);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-elev);
  box-shadow: 0 16px 36px -12px rgb(0 0 0 / 60%);
}

.permission-mode-option {
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

.permission-mode-option[data-highlighted] {
  background: var(--bg3);
}

.permission-mode-option:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}

.permission-mode-option__tick {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
  width: 18px;
  color: var(--accent);
}

.permission-mode-option__text {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
}

.permission-mode-option__name {
  font-size: var(--text-base);
  font-weight: 600;
}

.permission-mode-option__allows {
  font-size: var(--text-sm);
  color: var(--fg2);
  line-height: 1.3;
}

@media (pointer: coarse) {
  .permission-mode-trigger { min-height: var(--touch); }
}

@media (prefers-reduced-motion: reduce) {
  .permission-mode-trigger,
  .permission-mode-trigger__chevron { transition: none; }
}
</style>
