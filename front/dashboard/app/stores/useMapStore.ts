// Use https://code.highcharts.com/mapdata/ to download maps.

import { defineStore } from "pinia";
import { ref } from "vue";
import { registerMap } from "echarts/core";

export const useMapStore = defineStore("mapStore", () => {
  const registeredMaps = ref<Set<string>>(new Set());
  const isLoading = ref<boolean>(false);
  const error = ref<string | null>(null);

  const loadMapData = async (
    mapName: string,
    path: string,
  ): Promise<boolean> => {
    if (registeredMaps.value.has(mapName)) return true;

    isLoading.value = true;
    error.value = null;

    try {
      const response = await fetch(path);
      if (!response.ok) throw new Error(`Map "${mapName}" not found.`);

      const geoJson = await response.text();

      registerMap(mapName, geoJson);

      registeredMaps.value.add(mapName);
      return true;
    } catch (err) {
      error.value = err instanceof Error ? err.message : "unknown";
      console.error(err);
      return false;
    } finally {
      isLoading.value = false;
    }
  };

  return {
    isRegistered: (name: string) => registeredMaps.value.has(name),
    isLoading,
    error,
    loadMapData,
  };
});
