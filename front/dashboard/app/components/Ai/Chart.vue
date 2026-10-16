<script setup lang="ts">
import type { ChartSpec } from "~/utils/pengolo-markdown";
import { tableWorkbook, downloadBlob } from "~/utils/pengolo-export";
const props = defineProps<{ chart: ChartSpec }>();
const viewTable = ref(false);
const error = ref("");
const chartView = ref<{
  getDataURL(options: {
    type: "png";
    pixelRatio: number;
    backgroundColor: string;
  }): string;
} | null>(null);
const circular = computed(
  () => props.chart.type === "pie" || props.chart.type === "donut",
);
const rows = computed(() => [
  ["Label", ...props.chart.series.map((series) => series.name)],
  ...props.chart.labels.map((label, index) => [
    label,
    ...props.chart.series.map((series) => String(series.values[index])),
  ]),
]);
const options = computed(() => ({
  animation: false,
  color: [
    "#d97706",
    "#3b82f6",
    "#16a34a",
    "#a855f7",
    "#ea580c",
    "#0d9488",
    "#eab308",
    "#ec4899",
  ],
  tooltip: {
    trigger: circular.value ? "item" : "axis",
    renderMode: "richText",
  },
  legend: { bottom: 0, type: "scroll" },
  grid: { top: 20, left: 50, right: 20, bottom: 70, containLabel: true },
  ...(circular.value
    ? {}
    : {
        xAxis: { type: "category", data: props.chart.labels },
        yAxis: { type: "value" },
      }),
  series: props.chart.series.map((series) =>
    circular.value
      ? {
          name: series.name,
          type: "pie",
          radius: props.chart.type === "donut" ? ["38%", "65%"] : "65%",
          data: props.chart.labels.map((name, index) => ({
            name,
            value: series.values[index],
          })),
        }
      : {
          name: series.name,
          type: props.chart.type,
          data: series.values,
          itemStyle: series.color ? { color: series.color } : undefined,
        },
  ),
}));
async function spreadsheet() {
  try {
    downloadBlob(
      new Blob([await tableWorkbook(rows.value)], {
        type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      }),
      "pengolo-chart.xlsx",
    );
  } catch {
    error.value = "The spreadsheet could not be exported.";
  }
}
function image() {
  try {
    const url = chartView.value?.getDataURL({
      type: "png",
      pixelRatio: 2,
      backgroundColor: "#ffffff",
    });
    if (!url) throw new Error("Chart unavailable");
    const link = document.createElement("a");
    link.href = url;
    link.download = "pengolo-chart.png";
    link.click();
  } catch {
    error.value = "The chart could not be exported.";
  }
}
</script>

<template>
  <figure class="p-chart" :aria-label="chart.title || 'Chart'">
    <figcaption v-if="chart.title">{{ chart.title }}</figcaption>
    <div class="p-chart-actions">
      <UtilsButton variant="ghost" @click="viewTable = !viewTable">{{
        viewTable ? "View chart" : "View as table"
      }}</UtilsButton>
      <UtilsButton variant="ghost" @click="spreadsheet"
        >Download Excel</UtilsButton
      >
      <UtilsButton variant="ghost" :disabled="viewTable" @click="image"
        >Download image</UtilsButton
      >
    </div>
    <div v-if="viewTable" class="p-table">
      <table>
        <thead>
          <tr>
            <th v-for="(cell, index) in rows[0]" :key="index">{{ cell }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in rows.slice(1)" :key="index">
            <td v-for="(cell, column) in row" :key="column">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <VChart
      v-else
      ref="chartView"
      class="p-chart-view"
      :option="options"
      :init-options="{ renderer: 'canvas' }"
      autoresize
    />
    <p v-if="error" role="alert" class="p-error">{{ error }}</p>
  </figure>
</template>
