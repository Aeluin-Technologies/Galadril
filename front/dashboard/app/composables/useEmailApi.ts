import { trace, SpanStatusCode } from "@opentelemetry/api";
import { decoded } from "~/utils/email";

/** Email payloads bypass Apollo caching and never enter telemetry attributes. */
export function useEmailApi() {
  const controllers = new Set<AbortController>();
  function cancel() {
    for (const controller of controllers) controller.abort();
    controllers.clear();
  }
  onBeforeUnmount(cancel);
  onDeactivated(cancel);
  async function operation(request: Record<string, unknown>): Promise<unknown> {
    const controller = new AbortController();
    controllers.add(controller);
    const span = trace
      .getTracer("galadril.dashboard")
      .startSpan("email.request");
    try {
      const response = await $fetch<{
        data?: { emailOperation: unknown };
        errors?: Array<{ extensions?: { code?: string } }>;
      }>("/graphql", {
        method: "POST",
        credentials: "same-origin",
        signal: controller.signal,
        timeout: 190000,
        body: {
          query:
            "mutation Email($request:JSON!){emailOperation(request:$request)}",
          variables: { request: JSON.stringify(request) },
        },
      });
      if (response.errors?.length || !response.data)
        throw new Error(
          response.errors?.some(
            (error) => error.extensions?.code === "FORBIDDEN",
          )
            ? "FORBIDDEN"
            : "EMAIL_FAILED",
        );
      return decoded(response.data.emailOperation);
    } catch (error) {
      span.setStatus({ code: SpanStatusCode.ERROR });
      throw error;
    } finally {
      controllers.delete(controller);
      span.end();
    }
  }
  return { operation, cancel };
}
