<script setup lang="ts">
import { useMapStore } from "@/stores/useMapStore";
import { use, format } from "echarts/core";
import { record } from "../../../shared/records.ts";
import { CanvasRenderer } from "echarts/renderers";
import { ScatterChart, LinesChart } from "echarts/charts";
import {
  GeoComponent,
  TooltipComponent,
  TitleComponent,
} from "echarts/components";
import VChart from "vue-echarts";

const { t } = useI18n();

use([
  CanvasRenderer,
  ScatterChart,
  LinesChart,
  GeoComponent,
  TooltipComponent,
  TitleComponent,
]);

interface PointMeta {
  id: string | number;
  status: string;
  severity: "critical" | "warning" | "normal";
  country: string;
  issue?: string;
  target?: string;
}

interface MapPoint {
  name: string;
  value: [number, number];
  meta: PointMeta;
  targetCoords?: [number, number];
}

function pointMeta(value: unknown): PointMeta | undefined {
  const { id, status, severity, country, issue, target } = record(value);
  if (
    (typeof id !== "string" && typeof id !== "number") ||
    typeof status !== "string" ||
    typeof country !== "string" ||
    (severity !== "critical" && severity !== "warning" && severity !== "normal")
  )
    return;

  return {
    id,
    status,
    severity,
    country,
    ...(typeof issue === "string" ? { issue } : {}),
    ...(typeof target === "string" ? { target } : {}),
  };
}

const severityColors: Record<PointMeta["severity"], string> = {
  critical: "#ef4444",
  warning: "#f59e0b",
  normal: "#10b981",
};

const props = defineProps<{ points: MapPoint[] }>();
const emit = defineEmits<{ (e: "select-node", meta: PointMeta): void }>();

const mapStore = useMapStore();
const currentMap = ref<string>("World");

const filteredPoints = computed(() => {
  const activeMap = currentMap.value.trim().toLowerCase();
  if (activeMap === "world") return props.points || [];
  return (props.points || []).filter(
    (p) => p.meta?.country?.trim().toLowerCase() === activeMap,
  );
});

const chartOptions = computed(() => {
  if (!mapStore.isRegistered(currentMap.value)) return {};

  const points = filteredPoints.value;
  const linesData = points
    .filter((p) => p.value && p.targetCoords)
    .map((p) => ({
      coords: [p.value, p.targetCoords],
      lineStyle: {
        color: severityColors[p.meta.severity] || "#a1a1aa",
        width: p.meta.severity === "critical" ? 2.5 : 1.5,
        curveness: 0.3,
        opacity: 0.7,
      },
    }));

  return {
    backgroundColor: "transparent",
    geo: {
      map: currentMap.value,
      roam: true,
      silent: false,
      zoom: 1.1,
      label: {
        show: currentMap.value !== "World",
        fontSize: 10,
        fontWeight: 600,
        color: "#71717a",
      },
      itemStyle: {
        areaColor: "#f1f5f9",
        borderColor: "#cbd5e1",
        borderWidth: 1,
      },
      emphasis: {
        itemStyle: { areaColor: "#e2e8f0" },
        label: { show: true, color: "#18181b" },
      },
    },
    tooltip: {
      trigger: "item",
      backgroundColor: "rgba(255, 255, 255, 0.95)",
      borderWidth: 0,
      shadowBlur: 10,
      shadowColor: "rgba(0,0,0,0.1)",
      textStyle: { color: "#18181b" },
      formatter: (params: unknown) => {
        const point = record(params);
        const data = record(point.data);
        const meta = pointMeta(data.meta);
        if (!meta || point.seriesType === "lines") return "";
        const color = severityColors[meta.severity] || "#d4d4d8";
        return `
          <div style="padding: 4px;">
            <div style="font-weight: 800; font-size: 12px; text-transform: uppercase; margin-bottom: 4px; color: #18181b;">${format.encodeHTML(String(data.name ?? ""))}</div>
            <div style="font-size: 11px; color: #71717a; font-weight: 500;">
              ${t("map_component.status.tooltip_status")} : 
              <span style="color: ${color}; font-weight: 700;">${format.encodeHTML(meta.status)}</span>
            </div>
          </div>
        `;
      },
    },
    series: [
      {
        type: "lines",
        coordinateSystem: "geo",
        zlevel: 1,
        effect: {
          show: true,
          period: 4,
          trailLength: 0.4,
          symbol: "circle",
          symbolSize: 3,
          color: "#fff",
        },
        data: linesData,
      },
      {
        type: "scatter",
        coordinateSystem: "geo",
        zlevel: 2,
        data: points,
        symbolSize: 14,
        itemStyle: {
          color: (params: unknown) => {
            const meta = pointMeta(record(record(params).data).meta);
            return meta ? severityColors[meta.severity] : "#d4d4d8";
          },
          borderColor: "#fff",
          borderWidth: 2,
          shadowBlur: 6,
          shadowColor: "rgba(0,0,0,0.15)",
        },
        emphasis: {
          scale: true,
          itemStyle: {
            shadowBlur: 12,
            shadowColor: "rgba(0, 0, 0, 0.25)",
          },
        },
      },
    ],
  };
});

const onChartClick = async (params: unknown) => {
  const point = record(params);
  const meta = pointMeta(record(point.data).meta);
  if (point.componentType === "series" && meta) {
    emit("select-node", meta);
    return;
  }
  if (
    point.componentType === "geo" &&
    currentMap.value === "World" &&
    typeof point.name === "string"
  ) {
    const countryName = point.name;
    const success = await mapStore.loadMapData(
      countryName,
      `/geo/${countryName.toLowerCase()}.json`,
    );
    if (success) currentMap.value = countryName;
  }
};

const resetMap = () => (currentMap.value = "World");

onMounted(async () => {
  await mapStore.loadMapData("World", "/geo/world.json");
});
</script>

<template>
  <div
    class="w-full h-full bg-canvas border border-border rounded-xl overflow-hidden relative shadow-sm font-sans"
  >
    <div class="absolute top-4 left-4 z-10 flex flex-col gap-2">
      <div
        class="bg-surface/95 backdrop-blur-md border border-border px-4 py-3 shadow-lg rounded-lg pointer-events-none"
      >
        <h2 class="text-sm font-black text-foreground uppercase tracking-tight">
          {{
            currentMap === "World"
              ? $t("map_component.titles.global_title")
              : currentMap
          }}
        </h2>
        <p
          class="text-[10px] text-muted font-bold uppercase tracking-widest mt-0.5 opacity-80"
        >
          {{
            currentMap === "World"
              ? $t("map_component.titles.global_subtitle")
              : $t("map_component.titles.regional_subtitle")
          }}
        </p>
      </div>

      <UiButton
        v-if="currentMap !== 'World'"
        variant="ghost"
        class="w-fit uppercase flex items-center gap-2"
        @click="resetMap"
      >
        <span>←</span> {{ $t("map_component.controls.back_to_global") }}
      </UiButton>
    </div>

    <div class="w-full h-full">
      <VChart
        v-if="mapStore.isRegistered(currentMap)"
        :key="currentMap"
        :option="chartOptions"
        autoresize
        class="w-full h-full"
        @click="onChartClick"
      />
      <div
        v-else
        class="w-full h-full flex flex-col items-center justify-center gap-3 bg-surface"
      >
        <div
          class="w-8 h-8 border-4 border-border border-t-zinc-900 rounded-full animate-spin"
        />
        <span
          class="text-[10px] text-muted uppercase font-black tracking-widest"
        >
          {{ $t("map_component.status.loading") }}
        </span>
      </div>
    </div>

    <div
      v-if="mapStore.error"
      class="absolute bottom-4 right-4 bg-danger-subtle border border-danger text-danger text-[10px] px-4 py-2 rounded-lg shadow-md font-black uppercase"
    >
      {{ $t("map_component.status.error_unavailable") }}
    </div>
  </div>
</template>
