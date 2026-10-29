import { parseDefinition } from "./pipeline";
import type { AuthoringState } from "./pipeline";

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function object(value: unknown): Record<string, unknown> {
  if (isRecord(value)) return value;
  throw new Error("PIPELINE_AUTHORING_INVALID");
}
function text(value: unknown): string {
  if (typeof value === "string") return value;
  throw new Error("PIPELINE_AUTHORING_INVALID");
}
function number(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  throw new Error("PIPELINE_AUTHORING_INVALID");
}

/** The GraphQL JSON scalar remains untrusted even when cookies authenticate its source. */
export function parseAuthoring(input: unknown): AuthoringState {
  const value = object(typeof input === "string" ? JSON.parse(input) : input);
  const branches = Object.fromEntries(
    Object.entries(object(value.branches ?? {})).map(([id, input]) => {
      const branch = object(input);
      return [
        id,
        {
          definition: parseDefinition(branch.definition),
          base_definition: parseDefinition(branch.base_definition),
          base_revision_id: text(branch.base_revision_id),
          author_id: text(branch.author_id),
          updated_at_ms: number(branch.updated_at_ms),
        },
      ];
    }),
  );
  const proposals = Object.fromEntries(
    Object.entries(object(value.proposals ?? {})).map(([id, input]) => {
      const proposal = object(input);
      return [
        id,
        {
          source_branch: text(proposal.source_branch),
          source_hash: text(proposal.source_hash),
          target_hash: text(proposal.target_hash),
          author_id: text(proposal.author_id),
          created_at_ms: number(proposal.created_at_ms),
        },
      ];
    }),
  );
  const review = value.approval ? object(value.approval) : undefined;
  const release = value.release ? object(value.release) : undefined;
  return {
    branches,
    proposals,
    approval: review
      ? {
          definition_hash: text(review.definition_hash),
          author_id: text(review.author_id),
          reviewed_at_ms: number(review.reviewed_at_ms),
        }
      : null,
    release: release
      ? {
          definition_hash: text(release.definition_hash),
          revision_id: text(release.revision_id),
          author_id: text(release.author_id),
          published_at_ms: number(release.published_at_ms),
        }
      : null,
  };
}
