import type { PluginBlock } from "~/utils/plugin-blocks";
import { trace, SpanStatusCode } from "@opentelemetry/api";
import { parseDefinition } from "~/utils/pipeline";
import type {
  Definition,
  PipelineArtifact,
  PipelineSummary,
} from "~/utils/pipeline";

/** Requests remain on the authenticated Gateway; no artifact URL is client-owned. */
export function usePipelineCatalog() {
  const controllers = new Set<AbortController>();
  function cancel() {
    for (const controller of controllers) controller.abort();
    controllers.clear();
  }
  onBeforeUnmount(cancel);
  onDeactivated(cancel);

  async function query<T>(
    source: string,
    variables: Record<string, unknown>,
  ): Promise<T> {
    const controller = new AbortController();
    controllers.add(controller);
    const span = trace
      .getTracer("galadril.dashboard")
      .startSpan("pipeline.catalog.request");
    try {
      const response = await $fetch<{
        data?: T;
        errors?: Array<{ extensions?: { code?: string } }>;
      }>("/graphql", {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        timeout: 15000,
        body: { query: source, variables },
      });
      if (controller.signal.aborted) throw new Error("PIPELINE_CANCELLED");
      if (response.errors?.length || !response.data)
        throw new Error(
          response.errors?.some(
            (error) => error.extensions?.code === "FORBIDDEN",
          )
            ? "FORBIDDEN"
            : "PIPELINE_UNAVAILABLE",
        );
      return response.data;
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR });
      throw error;
    } finally {
      controllers.delete(controller);
      span.end();
    }
  }
  async function list() {
    const data = await query<{ pipelineLinkTargets: PipelineSummary[] }>(
      "{ pipelineLinkTargets(limit:100) { pipeline_id:pipelineId name head_revision_id:headRevisionId published_revision_id:publishedRevisionId } }",
      {},
    );
    return data.pipelineLinkTargets;
  }
  /** Authoring includes drafts; executable link targets remain publication-only. */
  async function browse() {
    const data = await query<{ pipelineDefinitions: PipelineSummary[] }>(
      `{ pipelineDefinitions(limit:100) { ${summaryFields} } }`,
      {},
    );
    return data.pipelineDefinitions;
  }
  const summaryFields =
    "pipeline_id:pipelineId name head_revision_id:headRevisionId published_revision_id:publishedRevisionId";
  const artifactFields = `${summaryFields} definition`;
  async function read(pipelineId: string, revisionId?: string) {
    const data = await query<{
      pipelineDefinition: (PipelineSummary & { definition: unknown }) | null;
    }>(
      `query($id:String!,$revision:String) { pipelineDefinition(pipelineId:$id,revisionId:$revision) { ${artifactFields} } }`,
      { id: pipelineId, revision: revisionId ?? null },
    );
    if (!data.pipelineDefinition) throw new Error("PIPELINE_UNAVAILABLE");
    return {
      ...data.pipelineDefinition,
      definition: parseDefinition(data.pipelineDefinition.definition),
    };
  }
  /** Compare-and-swap keeps a stale editor from replacing another author's version. */
  async function save(
    id: string,
    definition: Definition,
    expected?: string,
    message = "",
  ) {
    const field = expected ? "updatePipeline" : "createPipeline";
    const data = await query<
      Record<string, PipelineSummary & { definition: unknown }>
    >(
      `mutation($id:String!,$name:String!,$definition:JSON!,$message:String!${expected ? ",$expected:String!" : ""}) { ${field}(pipelineId:$id,name:$name,definition:$definition,message:$message${expected ? ",expectedHeadRevisionId:$expected" : ""}) { ${artifactFields} } }`,
      {
        id,
        name: definition.name,
        definition: JSON.stringify(definition),
        message,
        ...(expected ? { expected } : {}),
      },
    );
    const artifact = data[field];
    if (!artifact) throw new Error("PIPELINE_UNAVAILABLE");
    return { ...artifact, definition: parseDefinition(artifact.definition) };
  }
  async function publish(id: string, revision: string) {
    const data = await query<{
      publishPipeline: PipelineSummary & { definition: unknown };
    }>(
      `mutation($id:String!,$revision:String!) { publishPipeline(pipelineId:$id,revisionId:$revision) { ${artifactFields} } }`,
      { id, revision },
    );
    return {
      ...data.publishPipeline,
      definition: parseDefinition(data.publishPipeline.definition),
    };
  }
  async function remove(id: string, expected: string) {
    const data = await query<{ deletePipeline: boolean }>(
      "mutation($id:String!,$expected:String!) { deletePipeline(pipelineId:$id,expectedHeadRevisionId:$expected) }",
      { id, expected },
    );
    if (!data.deletePipeline) throw new Error("PIPELINE_UNAVAILABLE");
  }
  /** Registry cursors identify immutable commits; Gateway rechecks every read. */
  async function history(id: string, after?: string) {
    const data = await query<{
      pipelineDefinition: {
        history: {
          items: Array<{
            revision_id: string;
            name: string;
            message: string;
            author_id: string;
            updated_at_ms: number;
          }>;
          after: string | null;
        };
      } | null;
    }>(
      "query($id:String!,$after:String) { pipelineDefinition(pipelineId:$id) { history(after:$after) { items { revision_id:revisionId name message author_id:authorId updated_at_ms:updatedAtMs } after } } }",
      { id, after: after ?? null },
    );
    if (!data.pipelineDefinition?.history)
      throw new Error("PIPELINE_UNAVAILABLE");
    return data.pipelineDefinition.history;
  }
  async function validate(pipelineId: string, definition: Definition) {
    const data = await query<{
      pipelineComposition: {
        definition: unknown;
        blocks: number;
        transfers: number;
      };
    }>(
      "query($id:String!,$definition:JSON!) { pipelineComposition(pipelineId:$id, definition:$definition) { definition blocks transfers } }",
      { id: pipelineId, definition: JSON.stringify(definition) },
    );
    return {
      ...data.pipelineComposition,
      definition: parseDefinition(data.pipelineComposition.definition),
    };
  }
  async function models() {
    const data = await query<{
      scribeModels: { models: Array<{ alias: string; model: string }> };
    }>("{ scribeModels { models { alias model } } }", {});
    return data.scribeModels.models;
  }
  async function capabilities() {
    const data = await query<{
      pipelineCapabilities: { capabilities: PluginBlock[] };
    }>(
      "{ pipelineCapabilities { capabilities { id pluginId pluginVersion origin execution kind targets name description stepType parametersSchemaJson compatibility { allowedModels requires } } } }",
      {},
    );
    return data.pipelineCapabilities.capabilities;
  }
  return {
    list,
    browse,
    read,
    save,
    publish,
    remove,
    history,
    validate,
    models,
    capabilities,
    cancel,
  };
}
