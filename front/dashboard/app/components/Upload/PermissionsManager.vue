<script setup lang="ts">
import { PlusIcon, UserIcon, ShieldCheckIcon } from "@heroicons/vue/20/solid";
import { XMarkIcon } from "@heroicons/vue/24/outline";
import {
  useIamPermissions,
  type IamPermission,
} from "~/composables/useIamPermissions";

const props = withDefaults(
  defineProps<{
    modelValue: IamPermission[];
    simplePresets?: boolean;
  }>(),
  {
    simplePresets: false,
  },
);

const emit = defineEmits<{
  (e: "update:modelValue", value: IamPermission[]): void;
}>();

const { validateSpiceDbSyntax } = useIamPermissions();

const actionsPool = ["Read", "Write", "Delete", "Share", "Admin"];
const availablePresetRoles = ["Admin", "Manager", "Analyst", "Viewer"];

const targetType = ref<"user" | "role">("user");
const targetId = ref("");
const selectedAction = ref("Read");
const selectedEffect = ref<"allow" | "deny">("allow");
const localError = ref<string | null>(null);

/**
 * Evaluates and pushes a structured entry into the synchronized permission stack.
 * @param {string} [presetRole] - Optional hardcoded role string identifier bypass.
 * @returns {void}
 */
function addPermission(presetRole?: string): void {
  localError.value = null;
  const isPreset = !!presetRole;
  const targetName = isPreset ? presetRole : targetId.value.trim();

  if (!targetName) return;

  const newPermission: IamPermission = {
    effect: isPreset ? "allow" : selectedEffect.value,
    action: isPreset ? "Admin" : selectedAction.value,
    scope:
      isPreset || targetType.value === "role"
        ? { role: targetName }
        : { principal: targetName },
  };

  if (!validateSpiceDbSyntax(newPermission)) {
    localError.value = "Invalid SpiceDB naming convention syntax.";
    return;
  }

  const isDuplicate = props.modelValue.some(
    (p) =>
      p.effect === newPermission.effect &&
      p.action === newPermission.action &&
      p.scope.role === newPermission.scope.role &&
      p.scope.principal === newPermission.scope.principal,
  );

  if (!isDuplicate) {
    emit("update:modelValue", [...props.modelValue, newPermission]);
  }

  if (!isPreset) {
    targetId.value = "";
  }
}

/**
 * Removes an asset item rule from the reactive matrix array hierarchy.
 * @param {number} index - The literal index target to drop.
 * @returns {void}
 */
function removePermission(index: number): void {
  const updated = props.modelValue.filter((_, idx) => idx !== index);
  emit("update:modelValue", updated);
}
</script>

<template>
  <div
    class="space-y-4 bg-white p-5 rounded-xl border border-slate-200 shadow-sm"
  >
    <div class="text-xs font-semibold text-slate-500 uppercase tracking-wider">
      {{ $t("storage.permissions.title") }}
    </div>

    <div
      v-if="simplePresets"
      class="flex flex-wrap gap-2 items-center text-sm text-zinc-500"
    >
      <span>{{ $t("builder.roles.or_choose") }}</span>
      <button
        v-for="role in availablePresetRoles"
        :key="role"
        @click="addPermission(role)"
        type="button"
        class="text-xs px-2.5 py-1.5 bg-slate-100 text-slate-700 rounded-md hover:bg-slate-200 font-medium transition-colors"
      >
        + {{ role }}
      </button>
    </div>

    <div
      class="bg-slate-50 p-3 rounded-xl border border-slate-200/60 grid grid-cols-1 sm:grid-cols-12 gap-2 items-center"
    >
      <div class="sm:col-span-3">
        <select
          v-model="targetType"
          class="w-full text-xs bg-white border border-slate-200 rounded-lg px-2 py-1.5 focus:ring-1 focus:ring-amber-500 outline-none"
        >
          <option value="user">{{ $t("storage.permissions.user") }}</option>
          <option value="role">{{ $t("storage.permissions.role") }}</option>
        </select>
      </div>

      <div class="sm:col-span-4 relative">
        <component
          :is="targetType === 'user' ? UserIcon : ShieldCheckIcon"
          class="w-3.5 h-3.5 text-slate-400 absolute left-2.5 top-2.5"
        />
        <input
          v-model="targetId"
          @keydown.enter="addPermission()"
          type="text"
          :placeholder="
            targetType === 'user'
              ? $t('storage.permissions.placeholder_user')
              : $t('storage.permissions.placeholder_role')
          "
          class="w-full text-xs bg-white border border-slate-200 rounded-lg pl-7 pr-2 py-1.5 focus:ring-1 focus:ring-amber-500 outline-none placeholder:text-slate-400"
        />
      </div>

      <div class="sm:col-span-3">
        <select
          v-model="selectedAction"
          class="w-full text-xs bg-white border border-slate-200 rounded-lg px-2 py-1.5 focus:ring-1 focus:ring-amber-500 outline-none"
        >
          <option v-for="act in actionsPool" :key="act" :value="act">
            {{ act }}
          </option>
        </select>
      </div>

      <div class="sm:col-span-2">
        <button
          @click="addPermission()"
          type="button"
          class="w-full flex items-center justify-center space-x-1 bg-slate-900 hover:bg-slate-800 text-white rounded-lg px-2 py-1.5 text-xs font-medium transition-colors"
        >
          <PlusIcon class="w-3.5 h-3.5" />
          <span>{{ $t("storage.permissions.add") }}</span>
        </button>
      </div>
    </div>

    <div
      v-if="localError"
      class="text-xs text-red-500 bg-red-50 p-2 rounded-lg border border-red-200"
    >
      {{ localError }}
    </div>

    <div class="max-h-[180px] overflow-y-auto space-y-1.5">
      <div
        v-if="modelValue.length === 0"
        class="text-center py-4 text-xs text-slate-400 italic bg-slate-50/40 rounded-xl border border-dashed border-slate-200"
      >
        {{ $t("storage.permissions.no_restrictions") }}
      </div>

      <div
        v-for="(perm, idx) in modelValue"
        :key="idx"
        class="flex items-center justify-between bg-white border border-slate-100 px-3 py-2 rounded-lg text-xs shadow-sm hover:border-slate-200 transition-all"
      >
        <div class="flex items-center space-x-2">
          <span
            :class="[
              'px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider',
              perm.effect === 'allow'
                ? 'bg-green-50 text-green-700 border border-green-200'
                : 'bg-red-50 text-red-700 border border-red-200',
            ]"
          >
            {{ perm.effect }}
          </span>
          <span class="font-medium text-slate-700">
            {{ $t("storage.permissions.action") }}
            <code class="bg-slate-100 px-1 rounded font-mono text-[11px]">{{
              perm.action
            }}</code>
          </span>
          <span class="text-slate-400">•</span>
          <span class="text-slate-600 flex items-center space-x-1">
            <component
              :is="perm.scope.principal ? UserIcon : ShieldCheckIcon"
              class="w-3 h-3 text-slate-400"
            />
            <span>
              {{ $t("storage.permissions.target") }}
              <strong>{{ perm.scope.principal || perm.scope.role }}</strong>
            </span>
          </span>
        </div>

        <button
          @click="removePermission(idx)"
          type="button"
          class="text-slate-300 hover:text-red-500 p-1 rounded-md hover:bg-red-50 transition-colors"
        >
          <XMarkIcon class="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  </div>
</template>
