<script setup lang="ts">
const { t } = useI18n();

import { GridLayout, GridItem } from "grid-layout-plus";
import {
  PlusIcon,
  PencilSquareIcon,
  TrashIcon,
  ChartBarIcon,
  ArrowDownTrayIcon,
} from "@heroicons/vue/24/outline";
import { type IamPermission } from "~/composables/useIamPermissions";

interface LayoutItem {
  x: number;
  y: number;
  w: number;
  h: number;
  i: string;
  type: string;
  config: Record<string, any>;
}

const dashboardName = ref("");
const permissions = ref<IamPermission[]>([]);
const widgetIdCounter = ref(2);

const layout = ref<LayoutItem[]>([
  {
    x: 0,
    y: 0,
    w: 4,
    h: 4,
    i: "1",
    type: "Card",
    config: {
      title: t("administration.copy.new_indicator"),
      value: "0",
      trend: "Stable",
      icon: markRaw(ChartBarIcon),
    },
  },
]);

const isConfigModalOpen = ref(false);

const activeWidget = ref<LayoutItem | undefined>(undefined);

const addWidget = (type: string) => {
  let w = 4;
  let h = 4;
  let config: Record<string, any> = {};

  if (type === "Map") {
    w = 8;
    h = 12;
  } else if (type === "LineChart") {
    w = 8;
    h = 12;
    config = {
      title: t("administration.copy.new_chart"),
      data: [],
      categories: [],
      icon: markRaw(ChartBarIcon),
    };
  } else if (type === "Alerts") {
    w = 4;
    h = 8;
    config = { title: t("administration.copy.new_alerts"), alerts: [] };
  } else if (type === "Objects") {
    w = 12;
    h = 8;
    config = { title: t("administration.copy.new_objects"), items: [] };
  } else if (type === "Card") {
    config = {
      title: t("administration.copy.new_indicator"),
      value: "0",
      trend: "Stable",
      icon: markRaw(ChartBarIcon),
    };
  }

  const maxY = layout.value.reduce(
    (max, item) => Math.max(max, item.y + item.h),
    0,
  );

  layout.value.push({
    x: 0,
    y: maxY,
    w,
    h,
    i: String(widgetIdCounter.value++),
    type,
    config,
  });
};

const removeWidget = (id: string) => {
  layout.value = layout.value.filter((item) => item.i !== id);
};

const openConfig = (widget: LayoutItem) => {
  activeWidget.value = widget;
  isConfigModalOpen.value = true;
};

const saveConfig = (updatedWidget: LayoutItem) => {
  const index = layout.value.findIndex((item) => item.i === updatedWidget.i);
  if (index !== -1) layout.value[index] = updatedWidget;
};

const saveDashboard = () => {
  const exportPayload = {
    name: dashboardName.value || "Untitled Dashboard",
    permissions: permissions.value,
    layout: layout.value.map((item) => {
      const itemCopy = JSON.parse(JSON.stringify(item));

      if (item.config && "icon" in item.config && item.config.icon) {
        itemCopy.config.iconRef = "ChartBarIcon";
      }
      return itemCopy;
    }),
  };

  const dataStr =
    "data:text/json;charset=utf-8," +
    encodeURIComponent(JSON.stringify(exportPayload, null, 2));
  const downloadAnchorNode = document.createElement("a");
  downloadAnchorNode.setAttribute("href", dataStr);
  downloadAnchorNode.setAttribute(
    "download",
    `${exportPayload.name.replace(/\s+/g, "_")}.json`,
  );
  document.body.appendChild(downloadAnchorNode);
  downloadAnchorNode.click();
  downloadAnchorNode.remove();
};
</script>

<template>
  <div
    class="h-full overflow-y-auto p-6 bg-canvas font-sans space-y-6 overflow-x-hidden relative"
  >
    <header class="space-y-4">
      <div class="flex items-center justify-between">
        <UiInput
          v-model="dashboardName"
          :placeholder="$t('builder.title_placeholder')"
          class="w-1/2"
        />
        <div class="flex items-center gap-3">
          <UiButton variant="secondary" @click="saveDashboard">
            <ArrowDownTrayIcon class="w-4 h-4 mr-2" />
            {{ $t("builder.save_dashboard") }}
          </UiButton>

          <UtilsDropdown>
            <template #trigger>
              <UiButton variant="primary">
                <PlusIcon class="w-4 h-4 mr-2" /> {{ $t("builder.add_widget") }}
              </UiButton>
            </template>
            <UiButton
              @click="addWidget('Card')"
              variant="secondary"
              class="block w-full"
            >
              {{ $t("builder.widgets.card") }}
            </UiButton>
            <UiButton
              @click="addWidget('LineChart')"
              variant="secondary"
              class="block w-full"
            >
              {{ $t("builder.widgets.line_chart") }}
            </UiButton>
            <UiButton
              @click="addWidget('Alerts')"
              variant="secondary"
              class="block w-full"
            >
              {{ $t("builder.widgets.alerts") }}
            </UiButton>
            <UiButton
              @click="addWidget('Objects')"
              variant="secondary"
              class="block w-full"
            >
              {{ $t("builder.widgets.objects") }}
            </UiButton>
            <UiButton
              @click="addWidget('Map')"
              variant="ghost"
              class="block w-full"
            >
              {{ $t("builder.widgets.map") }}
            </UiButton>
          </UtilsDropdown>
        </div>
      </div>

      <UploadPermissionManager v-model="permissions" :simple-presets="true" />
    </header>

    <div
      class="bg-surface-hover/50 rounded-xl border border-border p-4 min-h-[600px] pb-32"
    >
      <GridLayout
        v-model:layout="layout"
        :col-num="12"
        :row-height="30"
        :is-draggable="true"
        :is-resizable="true"
        :vertical-compact="true"
        :margin="[16, 16]"
        :use-css-transforms="true"
      >
        <GridItem
          v-for="item in layout"
          :key="item.i"
          :x="item.x"
          :y="item.y"
          :w="item.w"
          :h="item.h"
          :i="item.i"
          class="group relative bg-surface border border-border rounded-xl shadow-sm hover:shadow-md transition-shadow cursor-move flex flex-col"
        >
          <div
            class="absolute top-2 right-2 z-[60] flex gap-1 opacity-0 group-hover:opacity-100 transition-opacity"
          >
            <UiIconButton
              @click="openConfig(item)"
              variant="secondary"
              :icon="PencilSquareIcon"
              :label="$t('ui.actions.edit')"
            />
            <UiIconButton
              @click="removeWidget(item.i)"
              variant="danger"
              :icon="TrashIcon"
              :label="$t('ui.actions.delete')"
            />
          </div>

          <div
            class="flex-1 w-full pointer-events-none overflow-hidden rounded-xl"
          >
            <WidgetCard
              v-if="item.type === 'Card'"
              v-bind="item.config"
              class="h-full border-none shadow-none bg-transparent"
            />
            <WidgetMap
              v-else-if="item.type === 'Map'"
              :points="[]"
              class="h-full border-none shadow-none rounded-none"
            />
            <WidgetLineChart
              v-else-if="item.type === 'LineChart'"
              v-bind="item.config"
              class="h-full border-none shadow-none rounded-none"
            />
            <WidgetAlerts
              v-else-if="item.type === 'Alerts'"
              v-bind="item.config"
              class="h-full border-none shadow-none rounded-none"
            />
            <WidgetObjects
              v-else-if="item.type === 'Objects'"
              v-bind="item.config"
              class="h-full border-none shadow-none rounded-none"
            />
          </div>
        </GridItem>
      </GridLayout>
    </div>

    <WidgetConfigModal
      :is-open="isConfigModalOpen"
      :widget="activeWidget"
      @close="isConfigModalOpen = false"
      @save="saveConfig"
    />
  </div>
</template>
