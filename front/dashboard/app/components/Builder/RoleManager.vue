<script setup>
import { ref } from "vue";
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
  <div class="bg-white p-5 rounded-xl border border-zinc-200 shadow-sm">
    <label class="block text-sm font-medium text-zinc-700 mb-2">
      {{ $t("builder.roles.label") }}
    </label>
    <div class="flex flex-wrap gap-2 mb-3">
      <span
        v-for="role in modelValue"
        :key="role"
        class="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-semibold bg-amber-50 text-amber-700 border border-amber-200"
      >
        {{ role }}
        <button
          @click="removeRole(role)"
          class="ml-1.5 text-amber-500 hover:text-amber-800 focus:outline-none"
        >
          <XMarkIcon class="w-3 h-3" />
        </button>
      </span>
    </div>
    <div class="flex items-center gap-3">
      <input
        v-model="roleInput"
        @keydown.enter="addRole()"
        :placeholder="$t('builder.roles.placeholder')"
        class="flex-1 text-sm border border-zinc-200 rounded-lg px-3 py-2 focus:ring-2 focus:ring-amber-500 focus:outline-none"
      />
      <div class="text-sm text-zinc-500">
        {{ $t("builder.roles.or_choose") }}
      </div>
      <button
        v-for="r in availableRoles.filter((ar) => !modelValue.includes(ar))"
        :key="r"
        @click="addRole(r)"
        class="text-xs px-2.5 py-1.5 bg-stone-100 text-zinc-600 rounded-md hover:bg-stone-200 font-medium transition-colors"
      >
        + {{ r }}
      </button>
    </div>
  </div>
</template>
