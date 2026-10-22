<script setup lang="ts">
import type { IdentityForm } from "~/types/auth";
defineProps<{ form: IdentityForm; busy: boolean }>();
const emit = defineEmits<{ submit: [event: SubmitEvent] }>();
</script>

<template>
  <form
    :action="form.action"
    method="post"
    class="space-y-5"
    :aria-busy="busy"
    @submit.prevent="emit('submit', $event)"
  >
    <UiAlert
      v-for="message in form.messages"
      :key="message.text"
      :tone="message.type === 'error' ? 'danger' : 'info'"
      >{{ message.text }}</UiAlert
    >
    <template v-for="node in form.nodes" :key="node.attributes.name">
      <template v-if="node.type === 'input'">
        <UiButton
          v-if="node.attributes.type === 'submit'"
          :name="node.attributes.name"
          :value="node.attributes.value"
          type="submit"
          :disabled="node.attributes.disabled"
          :loading="busy"
          class="w-full"
          >{{ $t("authentication.sign_in") }}</UiButton
        >
        <input
          v-else-if="node.attributes.type === 'hidden'"
          type="hidden"
          :name="node.attributes.name"
          :value="node.attributes.value"
        />
        <UiInput
          v-else-if="
            ['text', 'password', 'email'].includes(node.attributes.type)
          "
          :id="`local-${node.attributes.name}`"
          :name="node.attributes.name"
          :type="node.attributes.type"
          :value="node.attributes.value"
          :label="
            node.attributes.name === 'identifier'
              ? $t('authentication.email')
              : (node.meta.label?.text ?? node.attributes.name)
          "
          :required="node.attributes.required"
          :disabled="node.attributes.disabled"
          :autocomplete="
            node.attributes.type === 'password'
              ? 'current-password'
              : 'username'
          "
        />
      </template>
    </template>
  </form>
</template>
