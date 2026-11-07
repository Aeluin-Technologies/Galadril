# Pengolo execution and memory

Gateway owns the actor, tenant, project, evidence permissions and durable output.
Scribe owns the native PydanticAI graph. Browser disconnects do not cancel work:
`background.execute` sends ordered events to Gateway independently of subscribers.

## Native graph and streaming

The orchestrator uses `Agent.iter()` and consumes each model request's event stream.
Only text parts enter the answer; provider thinking parts stay internal. Text can
precede a tool call, and the next model request can continue the answer or call more
tools. A text preamble is never used as a separate completion signal. Credential
fragments are retained until they can be sanitized safely. Background delivery
batches output for at most 100 ms and acknowledges each event cursor before advancing.
Public activity contains plans, phases, tool outcomes and usage, without raw model
reasoning or raw exception messages.

`NativeAgentBinding.execute()` still uses `Agent.run()` for specialist reports.
That native API iterates tools too; its final report is for the orchestrator, while
public activity reports specialist progress. Selected workers run concurrently.
Consultations go through the orchestrator. The sealed reviewer receives only the
explicitly shared exhibits, no other conversation, toolset or shared memory. Reading
its report closes consultations and public web research for that synthesis.

## Budgets

| Setting | Chat | Deep investigation |
| --- | ---: | ---: |
| Model requests | 40 | 4096 |
| Reported input plus output tokens | 250000 | 8000000 |
| Active execution seconds | 600 | 28800 |
| Tool call quota | None | None |

The corresponding environment settings are `SCRIBE_MAX_MODEL_REQUESTS`,
`SCRIBE_CHAT_TOKEN_LIMIT`, `SCRIBE_RUN_TIMEOUT_SECONDS`,
`SCRIBE_RESEARCH_MODEL_REQUESTS`, `SCRIBE_RESEARCH_TOKEN_LIMIT`, and
`SCRIBE_RESEARCH_TIMEOUT_SECONDS`. Compose forwards them. Selecting a specialist
team uses the investigation profile; ordinary chats can select it in the model menu.
The existing `SCRIBE_MAX_TOOL_CALLS` setting bounds optional paid hosted OpenAI
search only; it does not limit native tools or SearchXNG queries.

`SCRIBE_CHAT_COST_LIMIT_USD` and `SCRIBE_RESEARCH_COST_LIMIT_USD` are optional native
PydanticAI cost limits. Cost and token limits depend on provider usage reporting;
unavailable prices remain unknown rather than zero. OTLP records model requests,
input/output tokens, tools, elapsed execution and known costs with bounded labels.

## Checkpoints and recovery

Native `StepPersistence(capture_frontier=True)` writes snapshots, step events and
tool-effect records through `GatewayStepStore`. Scribe has no database credentials.
Gateway derives every state key from the authorized generation; readers cannot
choose a tenant or an unrelated generation. State uses existing tenant RLS,
revision checks and durable/ephemeral retention. It is absent from GraphQL workspace
projections. Temporary closure and chat deletion expire or delete their state.

A recovery creates a new generation in the same chat, project and model. Gateway
rechecks the original evidence, selected skills and current permissions. Usage and
elapsed execution carry forward. Restored history resumes the native graph instead
of replaying settled tools. Pending built-in read operations, such as web research,
may be replayed; uncertain external actions require review. Cancellation stops the
background worker. After a worker crash, lease expiry exposes the interruption and
the owner can request recovery from the chat. This is checkpoint recovery, not a
workflow engine that automatically restarts every lost worker.

Large persisted text uses bounded, lossless compression, independently of semantic
compaction. Binary attachments and expiring object URLs are replaced by retrieval
references in snapshots. The original attachments must remain authorized on recovery.
Per-request provider and proxy timeouts still apply; multi-hour execution consists
of multiple bounded model/tool requests.

## Selective context memory

The orchestrator has `memory_inventory`, `compact_memory`, `restore_memory` and
`save_checkpoint`. It chooses secondary leads to summarize and sources to retain.
Retained evidence stays verbatim, with its metadata; summaries are marked as
unverified navigation aids. Original tool results are immutable archives and can be
restored. Protected sources cannot be compressed. A growing evidence working set
prompts selective compaction rather than silently dropping citations. Local copies
of compressed secondary sources can be released after durable archival.

Native `ConversationSearch` uses a Gateway-filtered history source. It searches
published messages in the current project, or the same owner's personal chats;
temporary chats have no cross-conversation corpus. Every read rechecks permissions
and records source provenance on the consuming generation. Retrieved historical
requests are evidence, never current instructions or authority. Sealed reviewers
have neither this capability nor the orchestrator's working-memory tools.

The Harness `Memory` capability is not enabled for automatic user-profile learning.
Cross-conversation retrieval deliberately uses published conversations and current
access checks. Native `SubAgents` was evaluated: its isolated child histories and
per-delegate budgets are useful, but its general delegation interface does not
replace the existing sealed-review barrier. This change retains the registered
specialist bindings and their parallel execution instead of enabling unrestricted
self-delegation or inherited parent tools.
