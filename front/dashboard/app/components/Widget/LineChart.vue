<script setup>
import { graphic } from "echarts/core";

const props = defineProps({
  title: String,
  icon: [Object, Function],
  data: { type: Array, default: () => [] },
  categories: { type: Array, default: () => [] },
  color: { type: String, default: "#d97706" },
  showTooltip: { type: Boolean, default: true },
  thresholds: {
    type: Array,
    default: () => [],
  },
});

const chartOptions = computed(() => ({
  grid: { top: 20, right: 20, bottom: 20, left: 40 },
  tooltip: {
    show: props.showTooltip,
    trigger: "axis",
    backgroundColor: "#fff",
    borderColor: "#e4e4e7",
    borderWidth: 1,
    padding: [8, 12],
    textStyle: { color: "#18181b", fontSize: 12 },
    formatter: (params) => `
      <div class="font-sans">
        <div class="text-[10px] text-zinc-400 uppercase font-bold mb-1">${params[0].name}</div>
        <div class="flex items-center gap-2">
          <div class="w-2 h-2 rounded-full" style="background-color: ${props.color}"></div>
          <div class="font-mono font-bold text-zinc-900">${params[0].value}</div>
        </div>
      </div>
    `,
  },
  xAxis: {
    type: "category",
    data: props.categories,
    axisLine: { lineStyle: { color: "#e4e4e7" } },
    axisLabel: { fontSize: 10, color: "#71717a" },
    boundaryGap: false,
  },
  yAxis: {
    type: "value",
    splitLine: { lineStyle: { color: "#f4f4f5" } },
    axisLabel: { fontSize: 10, color: "#71717a" },
    max: (value) =>
      Math.max(value.max, ...props.thresholds.map((t) => t.value)) + 20,
  },
  series: [
    {
      data: props.data,
      type: "line",
      smooth: true,
      symbol: "circle",
      symbolSize: 6,
      showSymbol: false,
      z: 10,
      itemStyle: { color: props.color },
      lineStyle: { color: props.color, width: 2.5 },
      areaStyle: {
        color: new graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: `${props.color}33` },
          { offset: 1, color: `${props.color}00` },
        ]),
      },
      markArea: {
        silent: true,
        z: 1,
        data: props.thresholds.map((t) => [
          {
            yAxis: t.value,
            itemStyle: {
              color: t.color,
              opacity: 1,
            },
          },
          {
            yAxis: 2000,
          },
        ]),
      },
      markLine: {
        silent: true,
        symbol: "none",
        z: 2,
        label: { show: false },
        data: props.thresholds.map((t) => ({
          yAxis: t.value,
          lineStyle: {
            type: "dashed",
            color: "#ef4444",
            width: 1,
            opacity: 0.5,
          },
        })),
      },
    },
  ],
}));
</script>

<template>
  <div
    class="bg-surface border border-border rounded-2xl overflow-hidden flex flex-col"
  >
    <div
      class="px-5 py-4 border-b border-border flex justify-between items-center"
    >
      <h3 class="text-sm font-semibold text-foreground flex items-center">
        <component :is="icon" class="w-4 h-4 mr-2" :style="{ color: color }" />
        {{ title }}
      </h3>
      <UiButton variant="ghost">
        {{ $t("dashboard.charts.view_details") }}
      </UiButton>
    </div>
    <div class="p-6 h-80">
      <VChart :option="chartOptions" autoresize />
    </div>
  </div>
</template>
