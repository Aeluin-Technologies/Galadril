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
  async function read(pipelineId: string, revisionId: string) {
    const data = await query<{
      pipelineDefinition: (PipelineSummary & { definition: unknown }) | null;
    }>(
      "query($id:String!,$revision:String!) { pipelineDefinition(pipelineId:$id,revisionId:$revision) { pipeline_id:pipelineId name head_revision_id:headRevisionId published_revision_id:publishedRevisionId definition } }",
      { id: pipelineId, revision: revisionId },
    );
    if (!data.pipelineDefinition) throw new Error("PIPELINE_UNAVAILABLE");
    return {
      ...data.pipelineDefinition,
      definition: parseDefinition(data.pipelineDefinition.definition),
    };
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
  return { list, read, validate, models, capabilities, cancel };
}
