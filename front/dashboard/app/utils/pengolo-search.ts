/** Search only bounded, already authorized snapshots; the server owns complete retrieval. */
import Fuse from "fuse.js";
import type { ChatRecord } from "./pengolo-api";

export interface LocalConversationHit {
  id: string;
  kind: "chat" | "message";
  chatId: string;
  messageId?: string;
  href: string;
  label: string;
  description: string;
  score: number;
}

interface SearchRow {
  kind: "chat" | "message";
  chatId: string;
  messageId?: string;
  title: string;
  text: string;
  expiresAt: number | null;
}

/** Copy truncated spans so a slice cannot retain a large source string. */
function boundedText(value: string, limit: number): string {
  if (value.length <= limit) return value;
  return new TextDecoder().decode(
    new TextEncoder().encode(value.slice(0, limit)),
  );
}

function conversationRows(
  chats: readonly ChatRecord[],
  active: ChatRecord | null,
  options: { temporary?: boolean; now?: number } = {},
) {
  const rows: SearchRow[] = [];
  const selected: ChatRecord[] = [];
  const seen = new Set<string>();
  let textBytes = 0;
  const now = options.now ?? Date.now();
  function select(chat: ChatRecord) {
    if (
      selected.length >= 128 ||
      seen.has(chat.id) ||
      !/^[a-f0-9]{32}$/.test(chat.id) ||
      (chat.expiresAt !== null && chat.expiresAt <= now)
    )
      return;
    seen.add(chat.id);
    selected.push(chat);
  }
  if (active && !options.temporary) select(active);
  for (let ordinal = 0; ordinal < Math.min(chats.length, 512); ordinal++) {
    const chat = chats.at(ordinal);
    if (chat) select(chat);
    if (selected.length === 128) break;
  }
  function append(row: SearchRow) {
    const bytes = row.text.length * 2;
    if (
      !row.text.trim() ||
      rows.length >= 192 ||
      textBytes + bytes > 128 * 1024
    )
      return;
    textBytes += bytes;
    rows.push(row);
  }
  for (const chat of selected) {
    const title = boundedText(chat.data.title, 256);
    append({
      kind: "chat",
      chatId: chat.id,
      title,
      text: title,
      expiresAt: chat.expiresAt,
    });
  }
  for (const chat of selected) {
    if (rows.length >= 192 || textBytes >= 128 * 1024) break;
    const messages = chat.data.messages ?? [];
    for (
      let ordinal = messages.length - 1;
      ordinal >= Math.max(0, messages.length - 10);
      ordinal--
    ) {
      if (rows.length >= 192 || textBytes >= 128 * 1024) break;
      const message = messages.at(ordinal);
      if (!message || !/^[A-Za-z0-9_-]{1,128}$/.test(message.id)) continue;
      append({
        kind: "message",
        chatId: chat.id,
        messageId: message.id,
        title: boundedText(chat.data.title, 256),
        text: boundedText(
          message.text,
          Math.min(2048, (128 * 1024 - textBytes) / 2),
        ),
        expiresAt: chat.expiresAt,
      });
    }
  }
  return { rows, textBytes };
}

/** Index a detached projection so eviction releases full conversation records. */
export function createConversationIndex(
  chats: readonly ChatRecord[],
  active: ChatRecord | null,
  options: { temporary?: boolean; now?: number } = {},
) {
  const { rows, textBytes } = conversationRows(chats, active, options);
  const fuse = new Fuse(rows, {
    keys: ["text"],
    threshold: 0.28,
    ignoreLocation: true,
    ignoreDiacritics: true,
    ignoreFieldNorm: true,
    minMatchCharLength: 2,
    includeScore: true,
    includeMatches: true,
  });
  return {
    stats: { entries: rows.length, textBytes },
    search(query: string, accessedAt = Date.now()): LocalConversationHit[] {
      if (query.length > 512) return [];
      const terms =
        query
          .match(/[\p{L}\p{M}\p{N}_]+/gu)
          ?.filter((word) => word.length >= 2) ?? [];
      if (
        !terms.length ||
        terms.length > 8 ||
        terms.some((word) => word.length > 32)
      )
        return [];
      return fuse
        .search({ $and: terms.map((term) => ({ text: term })) })
        .filter(
          ({ item }) => item.expiresAt === null || item.expiresAt > accessedAt,
        )
        .sort(
          (left, right) =>
            (left.score ?? 1) +
            (left.item.kind === "message" ? 0.05 : 0) -
            ((right.score ?? 1) + (right.item.kind === "message" ? 0.05 : 0)),
        )
        .slice(0, 12)
        .map(({ item, score, matches }) => {
          const position = matches?.at(0)?.indices.at(0)?.at(0) ?? 0;
          const start = Math.max(0, position - 50);
          return {
            id: `${item.kind}:${item.chatId}:${item.messageId ?? ""}`,
            kind: item.kind,
            chatId: item.chatId,
            messageId: item.messageId,
            href: `/pengolo/${item.chatId}${item.messageId ? `#p-message-${item.messageId}` : ""}`,
            label: item.title,
            description:
              item.kind === "message"
                ? item.text.slice(start, start + 180).replace(/\s+/g, " ")
                : "",
            score: score ?? 1,
          };
        });
    },
  };
}
