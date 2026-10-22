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
    class="space-y-4 bg-surface p-5 rounded-xl border border-border shadow-sm"
  >
    <div class="text-xs font-semibold text-muted uppercase tracking-wider">
      {{ $t("storage.permissions.title") }}
    </div>

    <div
      v-if="simplePresets"
      class="flex flex-wrap gap-2 items-center text-sm text-muted"
    >
      <span>{{ $t("builder.roles.or_choose") }}</span>
      <UiButton
        v-for="role in availablePresetRoles"
        :key="role"
        @click="addPermission(role)"
        type="button"
        variant="ghost"
      >
        + {{ role }}
      </UiButton>
    </div>

    <div
      class="bg-canvas p-3 rounded-xl border border-border/60 grid grid-cols-1 sm:grid-cols-12 gap-2 items-center"
    >
      <div class="sm:col-span-3">
        <UiSelect
          v-model="targetType"
          :options="[
            {
              label: String($t('storage.permissions.user')).trim(),
              value: 'user',
            },
            {
              label: String($t('storage.permissions.role')).trim(),
              value: 'role',
            },
          ]"
          class="w-full"
        />
      </div>

      <div class="sm:col-span-4 relative">
        <component
          :is="targetType === 'user' ? UserIcon : ShieldCheckIcon"
          class="w-3.5 h-3.5 text-muted absolute left-2.5 top-2.5"
        />
        <UiInput
          v-model="targetId"
          @keydown.enter="addPermission()"
          type="text"
          :placeholder="
            targetType === 'user'
              ? $t('storage.permissions.placeholder_user')
              : $t('storage.permissions.placeholder_role')
          "
          class="w-full placeholder:text-muted"
        />
      </div>

      <div class="sm:col-span-3">
        <UiSelect
          v-model="selectedAction"
          :options="[
            ...actionsPool.flatMap((act) => [
              { label: String(act).trim(), value: act },
            ]),
          ]"
          class="w-full"
        />
      </div>

      <div class="sm:col-span-2">
        <UiButton
          @click="addPermission()"
          type="button"
          variant="ghost"
          class="w-full flex items-center justify-center space-x-1"
        >
          <PlusIcon class="w-3.5 h-3.5" />
          <span>{{ $t("storage.permissions.add") }}</span>
        </UiButton>
      </div>
    </div>

    <div
      v-if="localError"
      class="text-xs text-danger bg-danger-subtle p-2 rounded-lg border border-danger"
    >
      {{ localError }}
    </div>

    <div class="max-h-[180px] overflow-y-auto space-y-1.5">
      <div
        v-if="modelValue.length === 0"
        class="text-center py-4 text-xs text-muted italic bg-canvas/40 rounded-xl border border-dashed border-border"
      >
        {{ $t("storage.permissions.no_restrictions") }}
      </div>

      <div
        v-for="(perm, idx) in modelValue"
        :key="idx"
        class="flex items-center justify-between bg-surface border border-border px-3 py-2 rounded-lg text-xs shadow-sm hover:border-border transition-all"
      >
        <div class="flex items-center space-x-2">
          <span
            :class="[
              'px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider',
              perm.effect === 'allow'
                ? 'bg-success-subtle text-success border border-success'
                : 'bg-danger-subtle text-danger border border-danger',
            ]"
          >
            {{ perm.effect }}
          </span>
          <span class="font-medium text-foreground">
            {{ $t("storage.permissions.action") }}
            <code class="bg-surface-muted px-1 rounded font-mono text-[11px]">{{
              perm.action
            }}</code>
          </span>
          <span class="text-muted">•</span>
          <span class="text-muted flex items-center space-x-1">
            <component
              :is="perm.scope.principal ? UserIcon : ShieldCheckIcon"
              class="w-3 h-3 text-muted"
            />
            <span>
              {{ $t("storage.permissions.target") }}
              <strong>{{ perm.scope.principal || perm.scope.role }}</strong>
            </span>
          </span>
        </div>

        <UiIconButton
          @click="removePermission(idx)"
          type="button"
          variant="danger"
          :icon="XMarkIcon"
          :label="$t('ui.actions.close')"
        />
      </div>
    </div>
  </div>
</template>
