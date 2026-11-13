<template>
  <span class="ciao-mark" aria-hidden="true"><span class="ciao-mark-ink"></span><span class="ciao-mark-dots"></span></span>
</template>

<style scoped>
.ciao-mark {
  display: block;
  width: 100%;
  height: 100%;
  position: relative;
}
.ciao-mark-ink, .ciao-mark-dots {
  position: absolute;
  inset: 0;
  mask-size: contain;
  mask-repeat: no-repeat;
  -webkit-mask-size: contain;
  -webkit-mask-repeat: no-repeat;
}
.ciao-mark-ink {
  background: currentColor;
  mask-image: url('/brand/ciaobot-mark-ink.png');
  -webkit-mask-image: url('/brand/ciaobot-mark-ink.png');
}
.ciao-mark-dots {
  background: var(--accent);
  mask-image: url('/brand/ciaobot-mark-dots.png');
  -webkit-mask-image: url('/brand/ciaobot-mark-dots.png');
}
</style>
