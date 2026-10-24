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
  async function remove(project: ProjectRecord) {
    const epoch = identityEpoch;
    await pengoloMutation("deletePengoloProject", {
      id: project.id,
      revision: project.revision,
    });
    if (epoch !== identityEpoch) throw new Error("Workspace identity changed");
    cache.clear();
    items.value = items.value.filter((item) => item.id !== project.id);
  }
  function clear() {
    identityEpoch++;
    cache.clear();
    items.value = [];
  }
  return { items, load, save, remove, clear };
});
