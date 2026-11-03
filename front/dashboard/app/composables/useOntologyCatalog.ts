import { trace, SpanStatusCode } from "@opentelemetry/api";
import type {
  SemanticDocument,
  TenantOntology,
  OntologyCommand,
} from "~/types/ontology";
import {
  parseSemanticDocument,
  parseSemanticPackages,
  parseOntologyWorkspace,
  parseOntologyHistory,
} from "~/utils/ontology";
export type {
  OntologyResource,
  SemanticDocument,
  SemanticPackage,
  TenantOntology,
} from "~/types/ontology";
export function useOntologyCatalog() {
  const controllers = new Set<AbortController>();
  function cancel() {
    for (const controller of controllers) controller.abort();
    controllers.clear();
  }
  onBeforeUnmount(cancel);
  onDeactivated(cancel);
  async function query<T>(
    source: string,
    variables: Record<string, unknown> = {},
  ): Promise<T> {
    const span = trace
      .getTracer("galadril.dashboard")
      .startSpan("ontology.catalog.request");
    const controller = new AbortController();
    controllers.add(controller);
    try {
      const response = await $fetch<{ data?: T; errors?: unknown[] }>(
        "/graphql",
        {
          method: "POST",
          credentials: "same-origin",
          signal: controller.signal,
          timeout: 20000,
          body: { query: source, variables },
        },
      );
      if (controller.signal.aborted)
        throw new Error("Ontology request cancelled");
      if (response.errors?.length || !response.data)
        throw new Error(
          "Ontology operation unavailable or denied. Reload if another author changed this ontology.",
        );
      return response.data;
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR });
      throw error;
    } finally {
      span.end();
      controllers.delete(controller);
    }
  }
  return {
    cancel,
    query,
    async canManage(
      id?: string,
      action: "CREATE" | "EDIT" | "PUBLISH" | "DELETE" = id ? "EDIT" : "CREATE",
    ) {
      const result = await query<{ ontologyCanManage: unknown }>(
        "query($id:String,$action:OntologyAccessAction!){ontologyCanManage(ontologyId:$id,action:$action)}",
        { id, action },
      );
      return result.ontologyCanManage === true;
    },
    async browse() {
      return (
        await query<{ ontologies: TenantOntology[] }>(
          "{ontologies{ontologyId displayName productionPublication{revisionId publicationId}}}",
        )
      ).ontologies;
    },
    async packages() {
      return parseSemanticPackages(
        (await query<{ pluginOntologies: unknown }>("{pluginOntologies}"))
          .pluginOntologies,
      );
    },
    async workspace(id: string, revision?: string) {
      return parseOntologyWorkspace(
        (
          await query<{ ontologyWorkspace: unknown }>(
            "query($id:String!,$revision:String){ontologyWorkspace(ontologyId:$id,revisionId:$revision)}",
            { id, revision },
          )
        ).ontologyWorkspace,
      );
    },
    async author(id: string, expected: string, command: OntologyCommand) {
      return parseOntologyWorkspace(
        (
          await query<{ authorOntology: unknown }>(
            "mutation($id:String!,$expected:String!,$command:JSON!){authorOntology(ontologyId:$id,expectedRevisionId:$expected,command:$command)}",
            { id, expected, command: JSON.stringify(command) },
          )
        ).authorOntology,
      );
    },
    async history(id: string, after?: string) {
      return parseOntologyHistory(
        (
          await query<{ ontologyHistory: unknown }>(
            "query($id:String!,$after:String){ontologyHistory(ontologyId:$id,afterRevisionId:$after,limit:20)}",
            { id, after },
          )
        ).ontologyHistory,
      );
    },
    async document(id: string, revision: string) {
      return parseSemanticDocument(
        (
          await query<{ ontologyDocument: unknown }>(
            "query($id:String!,$revision:String!){ontologyDocument(ontologyId:$id,revisionId:$revision)}",
            { id, revision },
          )
        ).ontologyDocument,
      );
    },
    async preview(
      packageId: string,
      packageVersion: string,
      targetId: string | undefined,
      targetRevision: string | undefined,
      mappings: Record<string, string>,
    ) {
      return parseSemanticDocument(
        (
          await query<{ pluginOntologyPreview: unknown }>(
            "query($packageId:String!,$packageVersion:String!,$targetId:String,$targetRevision:String,$mappings:JSON!){pluginOntologyPreview(packageId:$packageId,packageVersion:$packageVersion,targetOntologyId:$targetId,targetRevisionId:$targetRevision,mappings:$mappings)}",
            {
              packageId,
              packageVersion,
              targetId,
              targetRevision,
              mappings: JSON.stringify(mappings),
            },
          )
        ).pluginOntologyPreview,
      );
    },
    async save(
      id: string,
      name: string,
      packageId: string,
      packageVersion: string,
      targetId: string | undefined,
      targetRevision: string | undefined,
      mappings: Record<string, string>,
      expected: string | undefined,
      branch = "main",
    ) {
      return (
        await query<{ savePluginOntology: string }>(
          "mutation($id:String!,$name:String!,$packageId:String!,$packageVersion:String!,$targetId:String,$targetRevision:String,$mappings:JSON!,$expected:String,$branch:String){savePluginOntology(ontologyId:$id,displayName:$name,packageId:$packageId,packageVersion:$packageVersion,targetOntologyId:$targetId,targetRevisionId:$targetRevision,mappings:$mappings,expectedRevisionId:$expected,branch:$branch)}",
          {
            id,
            name,
            packageId,
            packageVersion,
            targetId,
            targetRevision,
            mappings: JSON.stringify(mappings),
            expected,
            branch,
          },
        )
      ).savePluginOntology;
    },
    async saveDraft(
      id: string,
      name: string,
      document: SemanticDocument,
      expected: string | undefined,
      branch = "main",
    ) {
      return (
        await query<{ saveOntologyDraft: string }>(
          "mutation($id:String!,$name:String!,$definition:JSON!,$expected:String,$branch:String){saveOntologyDraft(ontologyId:$id,displayName:$name,definition:$definition,expectedRevisionId:$expected,branch:$branch)}",
          { id, name, definition: JSON.stringify(document), expected, branch },
        )
      ).saveOntologyDraft;
    },
    async publish(id: string, name: string, revision: string) {
      await query(
        "mutation($id:String!,$name:String!,$revision:String!,$metadata:JSON!){publishOntology(ontologyId:$id,displayName:$name,revisionId:$revision,metadata:$metadata){revisionId}}",
        {
          id,
          name,
          revision,
          metadata: JSON.stringify({ source: "plugin_overlay" }),
        },
      );
    },
  };
}
