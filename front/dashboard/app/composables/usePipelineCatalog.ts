import {
  parsePreview,
  parseChanges,
  parseDiagnostics,
} from "~/utils/pipeline-review";
import { parseAuthoring } from "~/utils/pipeline-authoring";
import type { AuthoringCommand } from "~/utils/pipeline";
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
            : response.errors?.some(
                  (error) => error.extensions?.code === "CONFLICT",
                )
              ? "CONFLICT"
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
    "pipeline_id:pipelineId name head_revision_id:headRevisionId published_revision_id:publishedRevisionId lifecycle branch_names:branchNames";
  const artifactFields = `${summaryFields} definition authoring`;
  async function read(pipelineId: string, revisionId?: string) {
    const data = await query<{
      pipelineDefinition:
        (PipelineSummary & { definition: unknown; authoring: unknown }) | null;
    }>(
      `query($id:String!,$revision:String) { pipelineDefinition(pipelineId:$id,revisionId:$revision) { ${artifactFields} } }`,
      { id: pipelineId, revision: revisionId ?? null },
    );
    if (!data.pipelineDefinition) throw new Error("PIPELINE_UNAVAILABLE");
    return {
      ...data.pipelineDefinition,
      definition: parseDefinition(data.pipelineDefinition.definition),
      authoring: parseAuthoring(data.pipelineDefinition.authoring ?? {}),
    };
  }
  /** A draft command stores unfinished work; validation never precedes this mutation. */
  async function author(
    id: string,
    definition: Definition,
    command: AuthoringCommand,
    expected?: string,
    message = "",
  ) {
    const data = await query<{
      authorPipeline: PipelineSummary & {
        definition: unknown;
        authoring: unknown;
      };
    }>(
      `mutation($id:String!,$name:String!,$definition:JSON!,$command:JSON!,$message:String!,$expected:String) { authorPipeline(pipelineId:$id,name:$name,definition:$definition,command:$command,message:$message,expectedHeadRevisionId:$expected) { ${artifactFields} } }`,
      {
        id,
        name: definition.name,
        definition: JSON.stringify(definition),
        command: JSON.stringify(command),
        message: message.trim() || command.operation.replaceAll("_", " "),
        expected: expected ?? null,
      },
    );
    return {
      ...data.authorPipeline,
      definition: parseDefinition(data.authorPipeline.definition),
      authoring: parseAuthoring(data.authorPipeline.authoring),
    };
  }
  async function save(
    id: string,
    definition: Definition,
    expected?: string,
    message = "",
    branch = "main",
  ) {
    return author(
      id,
      definition,
      { operation: "save_draft", branch },
      expected,
      message,
    );
  }
  async function publish(id: string, revision: string) {
    const value = await read(id);
    return author(id, value.definition, { operation: "publish" }, revision);
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
  async function comparison(id: string, revision: string, branch = "main") {
    const data = await query<{ pipelineDefinition: { comparison: unknown } }>(
      "query($id:String!,$revision:String!,$branch:String) { pipelineDefinition(pipelineId:$id) { comparison(revisionId:$revision,branch:$branch) } }",
      { id, revision, branch },
    );
    return parseChanges(data.pipelineDefinition.comparison);
  }
  async function mergePreview(
    id: string,
    branch: string,
    resolutions: Record<string, "target" | "source"> = {},
  ) {
    const data = await query<{ pipelineDefinition: { mergePreview: unknown } }>(
      "query($id:String!,$branch:String!,$resolutions:JSON!) { pipelineDefinition(pipelineId:$id) { mergePreview(sourceBranch:$branch,resolutions:$resolutions) } }",
      { id, branch, resolutions: JSON.stringify(resolutions) },
    );
    return parsePreview(data.pipelineDefinition.mergePreview);
  }
  async function rebasePreview(
    id: string,
    base: string,
    branch: string,
    definition: Definition,
    resolutions: Record<string, "target" | "source"> = {},
  ) {
    const data = await query<{
      pipelineDefinition: PipelineSummary & {
        definition: unknown;
        authoring: unknown;
        rebasePreview: unknown;
      };
    }>(
      `query($id:String!,$base:String!,$branch:String!,$definition:JSON!,$resolutions:JSON!) { pipelineDefinition(pipelineId:$id) { ${artifactFields} rebasePreview(baseRevisionId:$base,branch:$branch,definition:$definition,resolutions:$resolutions) } }`,
      {
        id,
        base,
        branch,
        definition: JSON.stringify(definition),
        resolutions: JSON.stringify(resolutions),
      },
    );
    const item = data.pipelineDefinition;
    return {
      artifact: {
        ...item,
        definition: parseDefinition(item.definition),
        authoring: parseAuthoring(item.authoring),
      },
      preview: parsePreview(item.rebasePreview),
    };
  }
  async function diagnostics(id: string, definition: Definition) {
    const data = await query<{ pipelineDiagnostics: unknown }>(
      "query($id:String!,$definition:JSON!) { pipelineDiagnostics(pipelineId:$id,definition:$definition) }",
      { id, definition: JSON.stringify(definition) },
    );
    return parseDiagnostics(data.pipelineDiagnostics);
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
      "{ pipelineCapabilities { capabilities { id pluginId pluginVersion origin execution kind targets name description stepType modelClass parametersSchemaJson compatibility { allowedModels requires } } } }",
      {},
    );
    return data.pipelineCapabilities.capabilities;
  }
  return {
    list,
    browse,
    read,
    save,
    author,
    publish,
    remove,
    history,
    validate,
    comparison,
    mergePreview,
    rebasePreview,
    diagnostics,
    models,
    capabilities,
    cancel,
  };
}
