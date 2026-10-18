<script setup lang="ts">
import type { PythonExecution } from "~/utils/pengolo-activity";
const props = defineProps<{ executions: PythonExecution[] }>();
function image(execution: PythonExecution): string | null {
  if (
    execution.status !== "completed" ||
    execution.image.length > 174764 ||
    !/^[A-Za-z0-9+/]+={0,2}$/.test(execution.image)
  )
    return null;
  try {
    const header = atob(execution.image.slice(0, 44));
    if (
      !header.startsWith("\x89PNG\r\n\x1a\n") ||
      header.slice(12, 16) !== "IHDR"
    )
      return null;
    const width =
      ((header.charCodeAt(16) << 24) |
        (header.charCodeAt(17) << 16) |
        (header.charCodeAt(18) << 8) |
        header.charCodeAt(19)) >>>
      0;
    const height =
      ((header.charCodeAt(20) << 24) |
        (header.charCodeAt(21) << 16) |
        (header.charCodeAt(22) << 8) |
        header.charCodeAt(23)) >>>
      0;
    return width > 0 && width <= 2000 && height > 0 && height <= 2000
      ? `data:image/png;base64,${execution.image}`
      : null;
  } catch {
    return null;
  }
}
const displayed = computed(() =>
  props.executions.map((execution) => ({
    ...execution,
    imageUrl: image(execution),
  })),
);
</script>
<template>
  <section
    v-for="execution in displayed"
    :key="execution.id"
    class="p-python-execution"
    aria-label="Python execution"
  >
    <details>
      <summary>Python · {{ execution.status }}</summary>
      <pre
        aria-label="Executed Python code"
      ><code>{{ execution.code }}</code></pre>
      <pre v-if="execution.output" aria-label="Python output">{{
        execution.output
      }}</pre>
      <p v-if="execution.status === 'failed'" role="status">
        This calculation failed. The conversation can continue with other
        available results.
      </p>
    </details>
    <figure v-if="execution.imageUrl">
      <img
        :src="execution.imageUrl"
        alt="Chart produced by the Python calculation"
        loading="lazy"
      />
      <figcaption>Python calculation</figcaption>
    </figure>
  </section>
</template>
