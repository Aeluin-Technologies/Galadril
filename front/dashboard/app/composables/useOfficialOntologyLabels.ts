import type { OntologyResource } from "~/types/ontology";
import {
  officialOntologyPackageKey,
  officialOntologyResourceKey,
} from "~/utils/ontology";

export function useOfficialOntologyLabels() {
  const { t, te } = useI18n();
  return {
    packageLabel(packageId: string, fallback: string) {
      const scope = officialOntologyPackageKey(packageId);
      const key = scope && `${scope}.name`;
      return key && te(key) ? t(key) : fallback;
    },
    resourceLabel(
      resource: OntologyResource,
      packageId: string | undefined,
      preview = false,
    ) {
      const key = officialOntologyResourceKey(resource, packageId, preview);
      return key && te(key) ? t(key) : resource.display_name;
    },
  };
}
