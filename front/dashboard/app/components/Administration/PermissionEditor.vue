<script setup lang="ts">
const { t } = useI18n();

import {
  permissionBlocks,
  permissionManifest,
} from "~/utils/administration.mjs";

const model = defineModel<string>({ default: "" });
defineProps<{
  users: {
    userId: string;
    displayName?: string | null;
    email?: string | null;
  }[];
  roles: { roleName: string; displayName?: string | null }[];
  disabled?: boolean;
}>();
type Block =
  | { kind: "role_assignment"; user: string; role: string; enabled: boolean }
  | {
      kind: "relationship";
      resource: { type: string; id: string };
      relation: string;
      subject: { type: string; id: string };
      enabled: boolean;
    }
  | { kind: "cedar_policy"; id: string; content: string; active: boolean };
const mode = ref("blocks");
const blocks = ref<Block[]>([]);
const error = ref("");
function switchMode(next: string) {
  error.value = "";
  if (next === "blocks" && model.value.trim()) {
    try {
      blocks.value = permissionBlocks(model.value);
    } catch {
      error.value = t(
        "administration.copy.this_code_cannot_be_represented_as_supported_blocks",
      );
      return;
    }
  }
  mode.value = next;
}
function add(kind: Block["kind"]) {
  if (blocks.value.length >= 64) return;
  if (kind === "role_assignment")
    blocks.value.push({ kind, user: "", role: "", enabled: true });
  else if (kind === "relationship")
    blocks.value.push({
      kind,
      resource: { type: "group", id: "" },
      subject: { type: "user", id: "" },
      relation: "reader",
      enabled: true,
    });
  else blocks.value.push({ kind, id: "", content: "", active: true });
}
watch(
  blocks,
  (value) => {
    if (mode.value !== "blocks") return;
    error.value = "";
    try {
      model.value = JSON.stringify(permissionManifest(value), null, 2);
    } catch {
      model.value = "";
    }
  },
  { deep: true },
);
</script>

<template>
  <div class="space-y-4">
    <div
      class="flex items-center gap-1 rounded-lg bg-surface-muted p-1 w-fit"
      :aria-label="$t('administration.copy.permission_editor_mode')"
    >
      <UiButton
        type="button"
        :variant="mode === 'blocks' ? 'secondary' : 'ghost'"
        :aria-pressed="mode === 'blocks'"
        @click="switchMode('blocks')"
        >{{ $t("administration.copy.visual_blocks") }}</UiButton
      >
      <UiButton
        type="button"
        :variant="mode === 'code' ? 'secondary' : 'ghost'"
        :aria-pressed="mode === 'code'"
        @click="switchMode('code')"
        >{{ $t("administration.copy.code") }}</UiButton
      >
    </div>
    <p v-if="error" role="alert" class="text-sm text-danger">{{ error }}</p>
    <fieldset :disabled="disabled" class="min-w-0 space-y-4">
      <template v-if="mode === 'blocks'">
        <div
          v-if="!blocks.length"
          class="rounded-xl border border-dashed border-border-control bg-canvas p-6 text-center text-sm text-muted"
        >
          {{
            $t(
              "administration.copy.start_with_a_change_below_nothing_is_applied",
            )
          }}
        </div>
        <div
          v-for="(block, index) in blocks"
          :key="index"
          class="rounded-xl border border-border p-4 space-y-3"
        >
          <div class="flex justify-between items-center gap-3">
            <h3 class="text-sm font-semibold text-foreground">
              {{ index + 1 }}.
              {{
                block.kind === "role_assignment"
                  ? $t("administration.copy.assign_a_role")
                  : block.kind === "relationship"
                    ? $t("administration.copy.data_access")
                    : $t("administration.copy.advanced_policy")
              }}
            </h3>
            <UiButton
              type="button"
              variant="ghost"
              :aria-label="
                $t('administration.copy.remove_change_value0', {
                  value0: index + 1,
                })
              "
              @click="blocks.splice(index, 1)"
              >{{ $t("administration.copy.remove") }}</UiButton
            >
          </div>
          <template v-if="block.kind === 'role_assignment'">
            <label class="block text-xs font-medium text-muted"
              >{{ $t("administration.copy.person")
              }}<UiSelect
                v-model="block.user"
                required
                :options="[
                  {
                    label: $t('administration.copy.choose_a_person').trim(),
                    value: '',
                    disabled: true,
                  },
                  ...users.flatMap((user) => [
                    {
                      label: String(
                        user.displayName || user.email || user.userId,
                      ).trim(),
                      value: user.userId,
                    },
                  ]),
                ]"
            /></label>
            <label class="block text-xs font-medium text-muted"
              >{{ $t("storage.permissions.role")
              }}<UiSelect
                v-model="block.role"
                required
                :options="[
                  {
                    label: $t(
                      'administration.copy.choose_an_existing_role',
                    ).trim(),
                    value: '',
                    disabled: true,
                  },
                  ...roles.flatMap((role) => [
                    {
                      label: role.displayName || String(role.roleName).trim(),
                      value: String(role.roleName).trim(),
                    },
                  ]),
                ]"
            /></label>
            <label class="flex items-center gap-2 text-sm text-muted"
              ><UiCheckbox v-model="block.enabled" type="checkbox" />{{
                $t("administration.copy.grant_this_role_uncheck_to_remove_it")
              }}</label
            >
          </template>
          <template v-else-if="block.kind === 'relationship'">
            <label class="block text-xs font-medium text-muted"
              >{{ $t("administration.copy.person")
              }}<UiSelect
                v-model="block.subject.id"
                required
                :options="[
                  {
                    label: $t('administration.copy.choose_a_person').trim(),
                    value: '',
                    disabled: true,
                  },
                  ...users.flatMap((user) => [
                    {
                      label: String(
                        user.displayName || user.email || user.userId,
                      ).trim(),
                      value: user.userId,
                    },
                  ]),
                ]"
            /></label>
            <label class="block text-xs font-medium text-muted"
              >{{ $t("administration.copy.data_space_identifier")
              }}<UiInput
                v-model.trim="block.resource.id"
                required
                maxlength="64"
                :placeholder="
                  $t('administration.copy.provided_by_your_data_administrator')
                "
            /></label>
            <label class="block text-xs font-medium text-muted"
              >{{ $t("administration.copy.access")
              }}<UiSelect
                v-model="block.relation"
                :options="[
                  {
                    label: $t('administration.copy.view_data').trim(),
                    value: 'reader',
                  },
                  {
                    label: $t('administration.copy.add_data').trim(),
                    value: 'ingester',
                  },
                ]"
            /></label>
            <label class="flex items-center gap-2 text-sm text-muted"
              ><UiCheckbox v-model="block.enabled" type="checkbox" />{{
                $t("administration.copy.allow_this_access_uncheck_to_revoke_it")
              }}</label
            >
          </template>
          <template v-else>
            <label class="block text-xs font-medium text-muted"
              >{{ $t("administration.copy.policy_identifier")
              }}<UiInput v-model.trim="block.id" required maxlength="64"
            /></label>
            <label class="block text-xs font-medium text-muted"
              >{{ $t("administration.copy.cedar_policy")
              }}<UiTextarea
                v-model="block.content"
                required
                maxlength="8192"
                rows="6"
                spellcheck="false"
              />
            </label>
            <label class="flex items-center gap-2 text-sm text-muted"
              ><UiCheckbox v-model="block.active" type="checkbox" />{{
                $t("administration.copy.enable_this_policy")
              }}</label
            >
          </template>
        </div>
        <div class="flex flex-wrap gap-2">
          <UiButton
            type="button"
            variant="secondary"
            :disabled="blocks.length >= 64"
            @click="add('role_assignment')"
            >{{ $t("administration.copy.assign_a_role_2") }}</UiButton
          >
          <UiButton
            type="button"
            variant="secondary"
            :disabled="blocks.length >= 64"
            @click="add('relationship')"
            >{{ $t("administration.copy.data_access_2") }}</UiButton
          >
          <UiButton
            type="button"
            variant="ghost"
            :disabled="blocks.length >= 64"
            @click="add('cedar_policy')"
            >{{ $t("administration.copy.advanced_policy_2") }}</UiButton
          >
        </div>
      </template>
      <label v-else class="block text-sm font-medium text-foreground"
        >{{ $t("administration.copy.permission_manifest")
        }}<UiTextarea
          v-model="model"
          required
          maxlength="131072"
          rows="14"
          spellcheck="false"
          :placeholder="$t('administration.copy.version_1_operations')"
        />
      </label>
    </fieldset>
  </div>
</template>

<style scoped>
.admin-input {
  display: block;
  width: 100%;
  margin-top: 0.4rem;
  padding: 0.6rem 0.75rem;
  border: 1px solid #d4d4d8;
  border-radius: 0.5rem;
  background: white;
  color: #27272a;
  font-size: 0.875rem;
}
.admin-input:focus-visible {
  outline: 2px solid #d97706;
  outline-offset: 2px;
}
fieldset:disabled {
  opacity: 0.55;
}
</style>
