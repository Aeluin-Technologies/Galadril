<script setup lang="ts">
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
  roles: { roleName: string }[];
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
      error.value =
        "This code cannot be represented as supported blocks. Keep editing it in code mode.";
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
      class="flex items-center gap-1 rounded-lg bg-stone-100 p-1 w-fit"
      aria-label="Permission editor mode"
    >
      <UtilsButton
        type="button"
        :variant="mode === 'blocks' ? 'secondary' : 'ghost'"
        :aria-pressed="mode === 'blocks'"
        @click="switchMode('blocks')"
        >Visual blocks</UtilsButton
      >
      <UtilsButton
        type="button"
        :variant="mode === 'code' ? 'secondary' : 'ghost'"
        :aria-pressed="mode === 'code'"
        @click="switchMode('code')"
        >Code</UtilsButton
      >
    </div>
    <p v-if="error" role="alert" class="text-sm text-red-700">{{ error }}</p>
    <fieldset :disabled="disabled" class="min-w-0 space-y-4">
      <template v-if="mode === 'blocks'">
        <div
          v-if="!blocks.length"
          class="rounded-xl border border-dashed border-zinc-300 bg-stone-50 p-6 text-center text-sm text-zinc-500"
        >
          Start with a change below. Nothing is applied until you review and
          confirm.
        </div>
        <div
          v-for="(block, index) in blocks"
          :key="index"
          class="rounded-xl border border-zinc-200 p-4 space-y-3"
        >
          <div class="flex justify-between items-center gap-3">
            <h3 class="text-sm font-semibold text-zinc-900">
              {{ index + 1 }}.
              {{
                block.kind === "role_assignment"
                  ? "Assign a role"
                  : block.kind === "relationship"
                    ? "Data access"
                    : "Advanced policy"
              }}
            </h3>
            <UtilsButton
              type="button"
              variant="ghost"
              :aria-label="`Remove change ${index + 1}`"
              @click="blocks.splice(index, 1)"
              >Remove</UtilsButton
            >
          </div>
          <template v-if="block.kind === 'role_assignment'">
            <label class="block text-xs font-medium text-zinc-600"
              >Person<select v-model="block.user" required class="admin-input">
                <option disabled value="">Choose a person</option>
                <option
                  v-for="user in users"
                  :key="user.userId"
                  :value="user.userId"
                >
                  {{ user.displayName || user.email || user.userId }}
                </option>
              </select></label
            >
            <label class="block text-xs font-medium text-zinc-600"
              >Role<select v-model="block.role" required class="admin-input">
                <option disabled value="">Choose an existing role</option>
                <option v-for="role in roles" :key="role.roleName">
                  {{ role.roleName }}
                </option>
              </select></label
            >
            <label class="flex items-center gap-2 text-sm text-zinc-600"
              ><input
                v-model="block.enabled"
                type="checkbox"
                class="accent-amber-600"
              />Grant this role (uncheck to remove it)</label
            >
          </template>
          <template v-else-if="block.kind === 'relationship'">
            <label class="block text-xs font-medium text-zinc-600"
              >Person<select
                v-model="block.subject.id"
                required
                class="admin-input"
              >
                <option disabled value="">Choose a person</option>
                <option
                  v-for="user in users"
                  :key="user.userId"
                  :value="user.userId"
                >
                  {{ user.displayName || user.email || user.userId }}
                </option>
              </select></label
            >
            <label class="block text-xs font-medium text-zinc-600"
              >Data space identifier<input
                v-model.trim="block.resource.id"
                required
                maxlength="64"
                class="admin-input"
                placeholder="Provided by your data administrator"
            /></label>
            <label class="block text-xs font-medium text-zinc-600"
              >Access<select v-model="block.relation" class="admin-input">
                <option value="reader">View data</option>
                <option value="ingester">Add data</option>
              </select></label
            >
            <label class="flex items-center gap-2 text-sm text-zinc-600"
              ><input
                v-model="block.enabled"
                type="checkbox"
                class="accent-amber-600"
              />Allow this access (uncheck to revoke it)</label
            >
          </template>
          <template v-else>
            <label class="block text-xs font-medium text-zinc-600"
              >Policy identifier<input
                v-model.trim="block.id"
                required
                maxlength="64"
                class="admin-input"
            /></label>
            <label class="block text-xs font-medium text-zinc-600"
              >Cedar policy<textarea
                v-model="block.content"
                required
                maxlength="8192"
                rows="6"
                class="admin-input font-mono"
                spellcheck="false"
              />
            </label>
            <label class="flex items-center gap-2 text-sm text-zinc-600"
              ><input
                v-model="block.active"
                type="checkbox"
                class="accent-amber-600"
              />Enable this policy</label
            >
          </template>
        </div>
        <div class="flex flex-wrap gap-2">
          <UtilsButton
            type="button"
            variant="secondary"
            :disabled="blocks.length >= 64"
            @click="add('role_assignment')"
            >+ Assign a role</UtilsButton
          >
          <UtilsButton
            type="button"
            variant="secondary"
            :disabled="blocks.length >= 64"
            @click="add('relationship')"
            >+ Data access</UtilsButton
          >
          <UtilsButton
            type="button"
            variant="ghost"
            :disabled="blocks.length >= 64"
            @click="add('cedar_policy')"
            >+ Advanced policy</UtilsButton
          >
        </div>
      </template>
      <label v-else class="block text-sm font-medium text-zinc-700"
        >Permission manifest<textarea
          v-model="model"
          required
          maxlength="131072"
          rows="14"
          spellcheck="false"
          class="admin-input font-mono text-xs"
          placeholder='{"version":1,"operations":[]}'
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
