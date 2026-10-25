/** Tracks nested drag transitions without reading file contents before a drop. */
export class FileDragSession {
  #depth = 0;
  get active(): boolean {
    return this.#depth > 0;
  }
  enter(types: readonly string[]): boolean {
    if (this.active || types.includes("Files")) this.#depth++;
    return this.active;
  }
  leave(): boolean {
    this.#depth = Math.max(0, this.#depth - 1);
    return this.active;
  }
  reset(): void {
    this.#depth = 0;
  }
}
