<script setup>
import { XMarkIcon } from "@heroicons/vue/24/outline";

const props = defineProps({
  modelValue: {
    type: Array,
    required: true,
  },
});

const emit = defineEmits(["update:modelValue"]);

const availableRoles = ["Admin", "Manager", "Analyst", "Viewer"];
const roleInput = ref("");

const addRole = (role) => {
  const r = role || roleInput.value.trim();
  if (r && !props.modelValue.includes(r)) {
    emit("update:modelValue", [...props.modelValue, r]);
  }
  roleInput.value = "";
};

const removeRole = (role) => {
  emit(
    "update:modelValue",
    props.modelValue.filter((r) => r !== role),
  );
};
</script>

<template>
  <div class="bg-surface p-5 rounded-xl border border-border shadow-sm">
    <label class="block text-sm font-medium text-foreground mb-2">
      {{ $t("builder.roles.label") }}
    </label>
    <div class="flex flex-wrap gap-2 mb-3">
      <span
        v-for="role in modelValue"
        :key="role"
        class="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-semibold bg-primary-subtle text-primary-foreground border border-border"
      >
        {{ role }}
        <UiIconButton
          @click="removeRole(role)"
          variant="ghost"
          :icon="XMarkIcon"
          :label="$t('ui.actions.close')"
          class="ml-1.5"
        />
      </span>
    </div>
    <div class="flex items-center gap-3">
      <UiInput
        v-model="roleInput"
        @keydown.enter="addRole()"
        :placeholder="$t('builder.roles.placeholder')"
        class="flex-1"
      />
      <div class="text-sm text-muted">
        {{ $t("builder.roles.or_choose") }}
      </div>
      <UiButton
        v-for="r in availableRoles.filter((ar) => !modelValue.includes(ar))"
        :key="r"
        @click="addRole(r)"
        variant="ghost"
      >
        + {{ r }}
      </UiButton>
    </div>
  </div>
</template>
