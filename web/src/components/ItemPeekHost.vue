<script setup lang="ts">
/**
 * Where the item peek is drawn. Mounted once in ChatLayout, beside the file
 * viewer, and shows whichever peek the store holds.
 *
 * A route change closes the peek: it opened over the view it was on, and a
 * peek must not outlive that view. A kind added to `PeekTarget` gets its own
 * branch here.
 */
import { inject, watch } from 'vue'
import { routeLocationKey } from 'vue-router'
import TaskPeek from './TaskPeek.vue'
import { useItemPeekStore } from '../stores/itemPeek'

const peek = useItemPeekStore()
const route = inject(routeLocationKey, null)

watch(() => route?.fullPath, () => peek.close())
</script>

<template>
  <TaskPeek v-if="peek.target?.kind === 'task'" />
</template>
