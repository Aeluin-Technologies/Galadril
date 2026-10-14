import { defineStore } from "pinia";
import { createRequestCache, compatibleModels } from "~/utils/pengolo";
import { pengoloRequest } from "~/utils/pengolo-api";

export interface PengoloModel {
  alias: string;
  model: string;
  provider: string;
  reasoningEfforts: string[];
}

export const usePengoloModels = defineStore("pengoloModels", () => {
  const models = ref<PengoloModel[]>([]);
  const selected = ref("");
  const loading = ref(false);
  const error = ref("");
  const cache = createRequestCache<{
    models: PengoloModel[];
    defaultModel: string | null;
  }>();
  let identityEpoch = 0;
  async function load() {
    const epoch = identityEpoch;
    loading.value = true;
    error.value = "";
    try {
      const catalog = await cache.get(
        "catalog",
        () =>
          pengoloRequest<{
            models: PengoloModel[];
            defaultModel: string | null;
          }>(
            "query { pengoloModels { defaultModel models { alias model provider reasoningEfforts } } }",
          ),
        60_000,
      );
      if (epoch !== identityEpoch) return;
      error.value = "";
      models.value = compatibleModels(catalog);
      if (!models.value.some((model) => model.alias === selected.value))
        selected.value =
          models.value.find((model) => model.alias === catalog.defaultModel)
            ?.alias ??
          models.value[0]?.alias ??
          "";
    } catch {
      if (epoch === identityEpoch)
        error.value = "Models are unavailable. Try again.";
    } finally {
      if (epoch === identityEpoch) loading.value = false;
    }
  }
  function clear() {
    identityEpoch++;
    loading.value = false;
    error.value = "";
    cache.clear();
    models.value = [];
    selected.value = "";
  }
  return { models, selected, loading, error, load, clear };
});
