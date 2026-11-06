<script setup lang="ts">
import type { EvidenceBlock } from "~/utils/pengolo-markdown";

defineProps<{ block: EvidenceBlock }>();
</script>

<template>
  <figure class="p-evidence-block">
    <figcaption class="font-semibold">{{ block.title }}</figcaption>
    <div
      v-if="block.kind === 'risk'"
      class="overflow-x-auto"
      role="region"
      tabindex="0"
      :aria-label="block.title"
    >
      <table>
        <thead>
          <tr>
            <th scope="col">{{ $t("pengolo.analysis.issue") }}</th>
            <th scope="col">{{ $t("pengolo.analysis.priority") }}</th>
            <th scope="col">{{ $t("pengolo.analysis.assessment") }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(item, index) in block.items" :key="index">
            <th scope="row">{{ item.label }}</th>
            <td>
              <span :class="['p-evidence-priority', item.level]">{{
                $t(`pengolo.analysis.level.${item.level ?? "uncertain"}`)
              }}</span>
            </td>
            <td>
              {{ item.detail }}
              <small v-if="item.source" class="block text-muted">{{
                item.source
              }}</small>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <ol v-else role="list" :class="['p-evidence-steps', block.kind]">
      <li v-for="(item, index) in block.items" :key="index">
        <strong>{{ item.label }}</strong>
        <div>{{ item.detail }}</div>
        <small v-if="item.source" class="block text-muted">{{
          item.source
        }}</small>
      </li>
    </ol>
    <ul
      v-if="block.outcomes"
      class="p-evidence-outcomes"
      role="list"
      :aria-label="$t('pengolo.analysis.outcomes')"
    >
      <li v-for="(item, index) in block.outcomes" :key="index">
        <strong>{{ item.label }}</strong>
        <div>{{ item.detail }}</div>
        <small v-if="item.source" class="block text-muted">{{
          item.source
        }}</small>
      </li>
    </ul>
  </figure>
</template>
