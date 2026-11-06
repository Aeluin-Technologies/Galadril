import { computed } from "vue";
import { usePengoloAgents, type Specialist } from "~/stores/usePengoloAgents";
import { usePengoloModels } from "~/stores/usePengoloModels";
import { usePengoloUi } from "~/stores/usePengoloUi";
import { compatibleAgent } from "~/utils/pengolo";

interface SpecialistTeam {
  id: string;
  pluginId: string | null;
  name: string;
  description: string;
  members: Specialist[];
}

export function useSpecialistTeams(
  isCompatible: (agent: Specialist) => boolean = () => true,
) {
  const { t } = useI18n();
  const agents = usePengoloAgents();
  const models = usePengoloModels();
  const ui = usePengoloUi();
  const teams = computed(() => {
    const groups = new Map<string, SpecialistTeam>();
    for (const agent of agents.items) {
      const plugin = agent.data.pluginId || null;
      const id = plugin ? `plugin:${plugin}` : `personal:${agent.id}`;
      let team = groups.get(id);
      if (!team) {
        team = {
          id,
          pluginId: plugin,
          name:
            plugin === "law"
              ? t(`pengolo.specialists.plugins.${plugin}.name`)
              : plugin || agent.data.name,
          description:
            plugin === "law"
              ? t(`pengolo.specialists.plugins.${plugin}.description`)
              : plugin
                ? t("pengolo.specialists.pluginDescription")
                : agent.data.mission,
          members: [],
        };
        groups.set(id, team);
      }
      team.members.push(agent);
    }
    return [...groups.values()];
  });
  const activeTeams = computed(() =>
    teams.value.filter((team) =>
      team.members.some((member) => agents.selected.includes(member.id)),
    ),
  );
  function selected(team: SpecialistTeam) {
    return team.members.every((member) => agents.selected.includes(member.id));
  }
  function available(team: SpecialistTeam) {
    const project = ui.temporary ? null : ui.projectId;
    return (
      team.members.every(
        (member) =>
          isCompatible(member) &&
          compatibleAgent(member, project, models.selected, agents.catalog),
      ) &&
      new Set([...agents.selected, ...team.members.map((member) => member.id)])
        .size <= 8
    );
  }
  function toggle(team: SpecialistTeam) {
    const ids = new Set(team.members.map((member) => member.id));
    if (selected(team)) {
      agents.selected = agents.selected.filter((id) => !ids.has(id));
    } else if (available(team)) {
      agents.selected = [...new Set([...agents.selected, ...ids])];
    }
  }
  return { teams, activeTeams, selected, available, toggle };
}
