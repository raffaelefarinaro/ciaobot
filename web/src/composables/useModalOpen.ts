import { onBeforeUnmount, onMounted, readonly, ref, type Ref } from 'vue'

// True while any modal dialog (an element with aria-modal="true") is in the
// document. Observed on document.body so no dialog needs to cooperate.
export function useModalOpen(): Readonly<Ref<boolean>> {
  const open = ref(false)
  let observer: MutationObserver | null = null

  function update() {
    open.value = document.querySelector('[aria-modal="true"]') !== null
  }

  onMounted(() => {
    update()
    observer = new MutationObserver(update)
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ['aria-modal'],
    })
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    observer = null
  })

  return readonly(open)
}
