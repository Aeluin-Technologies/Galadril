<script setup lang="ts">
import { object, number, text } from "~/utils/email";
const props = defineProps<{ account: string; draft: string }>();
const emit = defineEmits<{ close: [] }>();
const api = useEmailApi();
const rows = shallowRef<
  Array<{ sequence: number; opened: number; ip: string }>
>([]);
const next = ref(0),
  loading = ref(false),
  failed = ref(false);
let epoch = 0;
async function load(append = false) {
  if (loading.value || (append && !next.value)) return;
  const own = ++epoch;
  loading.value = true;
  failed.value = false;
  try {
    const result = object(
      await api.operation({
        operation: "opens",
        account: props.account,
        draft: props.draft,
        before: append ? next.value : 0,
      }),
    );
    if (own !== epoch) return;
    if (!Array.isArray(result.items) || result.items.length > 25)
      throw new Error("INVALID_OPEN_HISTORY");
    const received = result.items.map((item) => {
      const row = object(item);
      return {
        sequence: number(row.sequence),
        opened: number(row.opened_at),
        ip: text(row.ip),
      };
    });
    rows.value = append ? [...rows.value, ...received] : received;
    next.value = number(result.next_before);
  } catch {
    if (own === epoch) {
      failed.value = true;
      next.value = 0;
    }
  } finally {
    if (own === epoch) loading.value = false;
  }
}
function more(event: Event) {
  const list = event.currentTarget;
  if (
    list instanceof HTMLElement &&
    list.scrollHeight - list.clientHeight - list.scrollTop < 100
  )
    void load(true);
}
function stop() {
  epoch++;
  api.cancel();
}
watch(
  () => [props.account, props.draft],
  () => {
    stop();
    loading.value = false;
    rows.value = [];
    void load();
  },
  { immediate: true },
);
onDeactivated(() => {
  stop();
  emit("close");
});
onBeforeUnmount(stop);
</script>
<template>
  <UiDialog
    :open="true"
    :title="$t('email.tracking.title')"
    :description="$t('email.tracking.help')"
    :close-label="$t('actions.close')"
    @close="$emit('close')"
  >
    <UiAlert v-if="failed" tone="danger"
      >{{ $t("email.tracking.failed")
      }}<UiButton variant="ghost" :action="() => load()">{{
        $t("email.retry")
      }}</UiButton></UiAlert
    >
    <p v-if="loading" role="status">{{ $t("email.loading") }}</p>
    <p v-else-if="!rows.length && !failed" class="text-sm text-muted">
      {{ $t("email.tracking.empty") }}
    </p>
    <div class="max-h-96 overflow-y-auto" @scroll.passive="more">
      <ul class="divide-y divide-border">
        <li
          v-for="row in rows"
          :key="row.sequence"
          class="flex flex-wrap items-center justify-between gap-3 py-3 text-sm"
        >
          <time :datetime="new Date(row.opened * 1000).toISOString()">{{
            new Date(row.opened * 1000).toLocaleString()
          }}</time>
          <code>{{ row.ip }}</code>
        </li>
      </ul>
    </div>
    <p class="mt-4 text-xs text-muted">{{ $t("email.tracking.retention") }}</p>
  </UiDialog>
</template>
