import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { record } from "./wire.ts";

/** Persists the source IP budget because Playwright replaces workers after failure. */
export class AuthenticationBudget {
  private remaining = Infinity;
  private resetAt = 0;

  private readonly path: string;

  constructor(path: string) {
    this.path = path;
    if (existsSync(path)) {
      const previous = record(JSON.parse(readFileSync(path, "utf8")));
      const reset = previous["x-ratelimit-reset"];
      const remaining = previous["x-ratelimit-remaining"];
      if (typeof reset === "string" && typeof remaining === "string") {
        this.observe({
          "x-ratelimit-reset": reset,
          "x-ratelimit-remaining": remaining,
        });
      }
    }
  }

  observe(headers: Readonly<Record<string, string>>): void {
    const reset = Number(headers["x-ratelimit-reset"]);
    const remaining = Number(headers["x-ratelimit-remaining"]);
    if (
      !Number.isSafeInteger(reset) ||
      reset <= 0 ||
      !Number.isSafeInteger(remaining) ||
      remaining < 0
    )
      return;
    if (reset < this.resetAt) return;
    this.remaining =
      reset === this.resetAt ? Math.min(this.remaining, remaining) : remaining;
    this.resetAt = reset;
    writeFileSync(
      this.path,
      JSON.stringify({
        "x-ratelimit-reset": String(this.resetAt),
        "x-ratelimit-remaining": String(this.remaining),
      }),
    );
  }

  delay(now = Date.now()): number {
    if (this.remaining >= 30) return 0;
    const delay = Math.max(0, this.resetAt - now + 100);
    if (delay > 60_100)
      throw new Error("Authentication rate limit reset exceeds one minute");
    return delay;
  }
}
