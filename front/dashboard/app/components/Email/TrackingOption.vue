<script setup lang="ts">
import { object } from "~/utils/email";
const enabled = defineModel<boolean>({ default: false });
defineProps<{ disabled?: boolean }>();
const available = ref(false);
const api = useEmailApi();
let epoch = 0;
async function load() {
  const own = ++epoch;
  try {
    const status = object(
      await api.operation({ operation: "tracking_status" }),
    );
    if (own === epoch) available.value = status.available === true;
  } catch {
    if (own === epoch) available.value = false;
  }
}
function stop() {
  epoch++;
  api.cancel();
}
onMounted(load);
onDeactivated(stop);
onBeforeUnmount(stop);
</script>
<template>
  <UiCheckbox
    v-model="enabled"
    :label="$t('email.tracking.enable')"
    :help="
      $t(available ? 'email.tracking.help' : 'email.tracking.unconfigured')
    "
    :disabled="disabled || (!available && !enabled)"
  />
</template>
