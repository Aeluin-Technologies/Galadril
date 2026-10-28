/** Re-read cursor pages together so inserts and deletes cannot leave gaps in the displayed window. */
export async function librarySnapshot<T extends { id: string }>(
  pages: number,
  fetchPage: (
    after: string | null,
  ) => Promise<{ items: T[]; after: string | null }>,
  current: () => boolean,
): Promise<{ items: T[]; after: string | null } | null> {
  const files = new Map<string, T>();
  let after: string | null = null;
  for (let index = 0; index < Math.max(1, pages); index++) {
    if (!current()) return null;
    const page = await fetchPage(after);
    if (!current()) return null;
    for (const file of page.items) files.set(file.id, file);
    after = page.after;
    if (after === null) break;
  }
  return { items: Array.from(files.values()), after };
}
