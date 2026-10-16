import { defineStore } from "pinia";
import { createRequestCache } from "~/utils/pengolo";
import { usePengoloWorkspace } from "~/stores/usePengoloWorkspace";
import {
  pengoloMutation,
  pengoloRequest,
  type ProjectRecord,
} from "~/utils/pengolo-api";

export const usePengoloProjects = defineStore("pengoloProjects", () => {
  const items = ref<ProjectRecord[]>([]);
  const cache = createRequestCache<ProjectRecord[]>();
  let identityEpoch = 0;
  async function load() {
    await usePengoloWorkspace().load();
  }
  async function save(input: Record<string, unknown>) {
    const epoch = identityEpoch;
    const project = await pengoloMutation<ProjectRecord>(
      "savePengoloProject",
      input,
    );
    if (epoch !== identityEpoch) throw new Error("Workspace identity changed");
    cache.clear();
    items.value = [
      ...items.value.filter((item) => item.id !== project.id),
      project,
    ];
    return project;
  }
  function clear() {
    identityEpoch++;
    cache.clear();
    items.value = [];
  }
  return { items, load, save, clear };
});
