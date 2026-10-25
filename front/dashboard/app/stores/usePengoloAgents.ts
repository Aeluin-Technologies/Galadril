import { defineStore } from "pinia";
import {
  pengoloData,
  pengoloMutation,
  pengoloOperation,
  pengoloRequest,
  type WorkspaceRecord,
} from "~/utils/pengolo-api";
import type { AgentSkill } from "~/utils/pengolo";

export type Specialist = WorkspaceRecord<{
  name: string;
  mission: string;
  skillIds: string[];
  readOnly?: boolean;
  pluginId?: string | null;
}>;
export interface SkillChoice extends AgentSkill {
  name: string;
  description: string;
  pluginId: string | null;
}

export const usePengoloAgents = defineStore("pengoloAgents", () => {
  const items = ref<Specialist[]>([]);
  const catalog = ref<SkillChoice[]>([]);
  const selected = ref<string[]>([]);
  let epoch = 0;
  async function load() {
    const current = epoch;
    const data = await pengoloData<{
      pengoloAgents: Specialist[];
      scribeCapabilities: { capabilities: SkillChoice[] };
    }>(pengoloOperation("Agents"));
    if (epoch !== current) return;
    items.value = data.pengoloAgents;
    catalog.value = data.scribeCapabilities.capabilities;
    selected.value = selected.value.filter((id) =>
      items.value.some((item) => item.id === id),
    );
  }
  async function save(input: Record<string, unknown>) {
    const current = epoch;
    const agent = await pengoloMutation<Specialist>("savePengoloAgent", input);
    if (epoch !== current) throw new Error("Workspace identity changed");
    items.value = [
      ...items.value.filter((item) => item.id !== agent.id),
      agent,
    ];
    return agent;
  }
  async function remove(id: string) {
    const current = epoch;
    await pengoloRequest<boolean>(pengoloOperation("deletePengoloAgent"), {
      id,
    });
    if (epoch !== current) return;
    items.value = items.value.filter((item) => item.id !== id);
    selected.value = selected.value.filter((item) => item !== id);
  }
  function clear() {
    epoch++;
    items.value = [];
    catalog.value = [];
    selected.value = [];
  }
  return { items, catalog, selected, load, save, remove, clear };
});
