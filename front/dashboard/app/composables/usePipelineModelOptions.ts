import { modelCategory } from "~/utils/plugin-blocks";
import type { PluginBlock } from "~/utils/plugin-blocks";

export function usePipelineModelOptions() {
  const { t, te } = useI18n();
  function modelLabel(item: PluginBlock): string {
    const key = `pipeline.models.names.${item.id.split(".").at(-1)}`;
    return item.pluginId === "core" && te(key) ? t(key) : item.name;
  }
  function modelOption(item: PluginBlock) {
    const key = `pipeline.models.purposes.${item.id.split(".").at(-1)}`;
    return {
      value: item.id,
      label: modelLabel(item),
      description:
        item.pluginId === "core" && te(key) ? t(key) : item.description,
      group: t(`pipeline.models.groups.${modelCategory(item)}`),
    };
  }
  return { modelLabel, modelOption };
}
