<script setup lang="ts">
import { useMapStore } from "@/stores/useMapStore";
import { use } from "echarts/core";
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
      formatter: (params: any) => {
        if (!params.data || params.seriesType === "lines") return "";
        const meta = params.data.meta as PointMeta;
        const color = severityColors[meta.severity] || "#d4d4d8";
        return `
          <div style="padding: 4px;">
            <div style="font-weight: 800; font-size: 12px; text-transform: uppercase; margin-bottom: 4px; color: #18181b;">${params.data.name}</div>
            <div style="font-size: 11px; color: #71717a; font-weight: 500;">
              ${t("map_component.status.tooltip_status")} : 
              <span style="color: ${color}; font-weight: 700;">${meta.status}</span>
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
          color: (params: any) =>
            severityColors[
              params.data?.meta?.severity as PointMeta["severity"]
            ] || "#d4d4d8",
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

const onChartClick = async (params: any) => {
  if (params.componentType === "series" && params.data?.meta) {
    emit("select-node", params.data.meta);
    return;
  }
  if (params.componentType === "geo" && currentMap.value === "World") {
    const countryName = params.name;
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
    class="w-full h-full bg-slate-50 border border-zinc-200 rounded-xl overflow-hidden relative shadow-sm font-sans"
  >
    <div class="absolute top-4 left-4 z-10 flex flex-col gap-2">
      <div
        class="bg-white/95 backdrop-blur-md border border-zinc-200 px-4 py-3 shadow-lg rounded-lg pointer-events-none"
      >
        <h2 class="text-sm font-black text-zinc-900 uppercase tracking-tight">
          {{
            currentMap === "World"
              ? $t("map_component.titles.global_title")
              : currentMap
          }}
        </h2>
        <p
          class="text-[10px] text-zinc-500 font-bold uppercase tracking-widest mt-0.5 opacity-80"
        >
          {{
            currentMap === "World"
              ? $t("map_component.titles.global_subtitle")
              : $t("map_component.titles.regional_subtitle")
          }}
        </p>
      </div>

      <button
        v-if="currentMap !== 'World'"
        @click="resetMap"
        class="w-fit bg-stone-900 text-white text-[10px] px-3 py-2 rounded-md hover:bg-stone-700 transition-all uppercase font-black flex items-center gap-2 shadow-md active:scale-95"
      >
        <span>←</span> {{ $t("map_component.controls.back_to_global") }}
      </button>
    </div>

    <div class="w-full h-full">
      <VChart
        v-if="mapStore.isRegistered(currentMap)"
        :key="currentMap"
        :option="chartOptions"
        autoresize
        @click="onChartClick"
        class="w-full h-full"
      />
      <div
        v-else
        class="w-full h-full flex flex-col items-center justify-center gap-3 bg-white"
      >
        <div
          class="w-8 h-8 border-4 border-zinc-100 border-t-zinc-900 rounded-full animate-spin"
        ></div>
        <span
          class="text-[10px] text-zinc-500 uppercase font-black tracking-widest"
        >
          {{ $t("map_component.status.loading") }}
        </span>
      </div>
    </div>

    <div
      v-if="mapStore.error"
      class="absolute bottom-4 right-4 bg-red-50 border border-red-200 text-red-600 text-[10px] px-4 py-2 rounded-lg shadow-md font-black uppercase"
    >
      {{ $t("map_component.status.error_unavailable") }}
    </div>
  </div>
</template>
