/** Requests only GOL DTOs and never logs source text or protected metadata. */
import { trace, SpanStatusCode } from "@opentelemetry/api";
import type { GolSchemaProfile, GolSourceFile } from "~/types/ontology";
import {
  parseGolSummaries,
  parseGolWorkspace,
  parseGolValidation,
  parseGolPublication,
  parseGolSymbols,
} from "~/utils/ontology";

const summary =
  "ontologyId displayName sourceRevision activeGeneration activeSourceRevision status";
const diagnostics = "code message path start end severity";
const dependencies = "ontologyId generation sourceRevision";
const workspace = `summary{${summary}} files{path text} diagnostics{${diagnostics}} dependencies{${dependencies}}`;
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
    const span = trace.getTracer("galadril.dashboard").startSpan("gol.request");
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
      if (
        controller.signal.aborted ||
        response.errors?.length ||
        !response.data
      )
        throw new Error("GOL_UNAVAILABLE");
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
    async canManage(
      id?: string,
      action: "CREATE" | "EDIT" | "PUBLISH" | "DELETE" = id ? "EDIT" : "CREATE",
    ) {
      return (
        (
          await query<{ golOntologyCanManage: boolean }>(
            "query($ontologyId:String,$action:GolOntologyAccessAction!){golOntologyCanManage(ontologyId:$ontologyId,action:$action)}",
            { ontologyId: id, action },
          )
        ).golOntologyCanManage === true
      );
    },
    async browse() {
      return parseGolSummaries(
        (
          await query<{ golOntologies: unknown }>(
            `{golOntologies(limit:100){${summary}}}`,
          )
        ).golOntologies,
      );
    },
    async workspace(ontologyId: string) {
      return parseGolWorkspace(
        (
          await query<{ golOntologyWorkspace: unknown }>(
            `query($ontologyId:String!){golOntologyWorkspace(ontologyId:$ontologyId){${workspace}}}`,
            { ontologyId },
          )
        ).golOntologyWorkspace,
      );
    },
    async save(
      ontologyId: string,
      displayName: string,
      files: readonly GolSourceFile[],
      expectedRevision?: string,
    ) {
      return parseGolWorkspace(
        (
          await query<{ saveGolOntology: unknown }>(
            `mutation($ontologyId:String!,$displayName:String!,$files:[GolSourceFileInput!]!,$expectedRevision:String){saveGolOntology(ontologyId:$ontologyId,displayName:$displayName,files:$files,expectedRevision:$expectedRevision){${workspace}}}`,
            { ontologyId, displayName, files, expectedRevision },
          )
        ).saveGolOntology,
      );
    },
    async validate(ontologyId: string, sourceRevision: string) {
      return parseGolValidation(
        (
          await query<{ validateGolOntology: unknown }>(
            `mutation($ontologyId:String!,$sourceRevision:String!){validateGolOntology(ontologyId:$ontologyId,sourceRevision:$sourceRevision){ontologyId sourceRevision status diagnostics{${diagnostics}} dependencies{${dependencies}}}}`,
            { ontologyId, sourceRevision },
          )
        ).validateGolOntology,
      );
    },
    async publish(
      ontologyId: string,
      sourceRevision: string,
      expectedGeneration?: string,
    ) {
      return parseGolPublication(
        (
          await query<{ publishGolOntology: unknown }>(
            "mutation($ontologyId:String!,$sourceRevision:String!,$expectedGeneration:String){publishGolOntology(ontologyId:$ontologyId,sourceRevision:$sourceRevision,expectedGeneration:$expectedGeneration){ontologyId sourceRevision generation}}",
            { ontologyId, sourceRevision, expectedGeneration },
          )
        ).publishGolOntology,
      );
    },
    async remove(ontologyId: string, expectedRevision: string) {
      await query(
        "mutation($ontologyId:String!,$expectedRevision:String!){deleteGolOntology(ontologyId:$ontologyId,expectedRevision:$expectedRevision){revision}}",
        { ontologyId, expectedRevision },
      );
    },
    async symbols(ontologyId: string, generation?: string) {
      return parseGolSymbols(
        (
          await query<{ golOntologySymbols: unknown }>(
            "query($ontologyId:String!,$generation:String){golOntologySymbols(ontologyId:$ontologyId,generation:$generation){generation symbols{key name kind references properties{key name typeName}}}}",
            { ontologyId, generation },
          )
        ).golOntologySymbols,
      );
    },
    async schema(
      ontologyId: string,
      generation: string,
      target: string,
      profile: GolSchemaProfile,
      fields: readonly string[],
    ): Promise<unknown> {
      const value = (
        await query<{ golOntologySchema: unknown }>(
          "query($ontologyId:String!,$generation:String!,$target:String!,$profile:GolSchemaProfile!,$fields:[String!]!){golOntologySchema(ontologyId:$ontologyId,generation:$generation,target:$target,profile:$profile,fields:$fields)}",
          { ontologyId, generation, target, profile, fields },
        )
      ).golOntologySchema;
      return typeof value === "string" ? JSON.parse(value) : value;
    },
  };
}
