<script setup lang="ts">
import DOMPurify from "dompurify";

const props = defineProps<{ html: string }>();
const target = ref<HTMLDivElement | null>(null);

// The only DOM write receives nodes returned by the sanitizer.
watchEffect(
  () => {
    const element = target.value;
    if (!element) return;

    const fragment = DOMPurify.sanitize(props.html, {
      ADD_ATTR: ["target", "referrerpolicy"],
      RETURN_DOM_FRAGMENT: true,
    });
    element.replaceChildren(fragment);
  },
  { flush: "post" },
);
</script>

<template>
  <div ref="target" />
</template>
