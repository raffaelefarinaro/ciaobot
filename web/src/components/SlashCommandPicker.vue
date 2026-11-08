<template>
  <div class="commands-picker" role="listbox" aria-label="Slash commands">
    <div
      v-for="(cmd, i) in commands"
      :key="cmd.name"
      class="commands-picker-row"
      :class="{ active: i === highlight }"
      role="option"
      :aria-selected="i === highlight"
      @mousedown.prevent="$emit('pick', cmd)"
      @mouseenter="$emit('highlight', i)"
    >
      <div class="commands-picker-head">
        <span v-if="cmd.source === 'skill'" class="commands-picker-kind">skill</span>
        <span class="commands-picker-name">/{{ cmd.name }}</span>
        <span v-if="cmd.argument_hint" class="commands-picker-hint">{{ cmd.argument_hint }}</span>
      </div>
      <div v-if="cmd.description" class="commands-picker-desc">{{ cmd.description }}</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { SlashCommand } from '../lib/types'

defineProps<{
  commands: SlashCommand[]
  highlight: number
}>()

defineEmits<{
  pick: [cmd: SlashCommand]
  highlight: [index: number]
}>()
</script>

<style scoped src="./slashCommands.css"></style>
