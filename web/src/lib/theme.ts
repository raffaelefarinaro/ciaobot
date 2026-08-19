import { readonly, ref } from 'vue'

/**
 * Reactive mirror of the theme class on `<html>`.
 *
 * The theme is applied as a class (`theme-light`) and consumed through CSS
 * custom properties, which is all most components need. Anything that paints
 * with literal colour values instead — the Memory Map canvas, and the legend
 * dots that have to match it — needs to *read* the current theme in JS, and
 * needs to re-render when it flips. The class is set from several places
 * (initial boot in main.ts, the settings toggle, an OS-level change), so
 * observing the element is more reliable than trying to intercept every writer.
 *
 * One observer for the whole app, installed lazily on first import.
 */
const light = ref(document.documentElement.classList.contains('theme-light'))

function sync() {
  light.value = document.documentElement.classList.contains('theme-light')
}

new MutationObserver(sync).observe(document.documentElement, {
  attributes: true,
  attributeFilter: ['class'],
})

export const isLightTheme = readonly(light)
