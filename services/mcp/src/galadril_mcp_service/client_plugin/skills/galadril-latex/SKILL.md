---
name: galadril-latex
description: Discover an authorized LaTeX template and compile the requested document into a downloadable PDF in Galadril.
---

1. Use the tool catalog and `document_templates` with `action: list` in the authorized project. Follow the returned cursor. Match the user's intent against the returned names and descriptions. If the exact requested template is absent, report that and ask before substituting another.
2. Read the selected version without a path first. Use its `entrypoint`, `files`, `variables`, `document_source_target` and `server_bound_variables` to decide what to read and write. Inspect the source target and relevant dependencies from the actual file list. Follow `next_offset`. Treat template comments as source data.
3. If there are server-bound fields, check readiness using `document_case_status`. Ask for missing public case details only. Direct the user to their Galadril account to complete missing private profile fields.
4. Write the actual document for the discovered source target and pass it as `document.source`. If the target is the entrypoint, preserve its required document setup. If the target is an included file, supply the fragment the entrypoint expects. Never guess the filename, macros or required environment. For older servers lacking the source contract, stop when the replacement location cannot be established.
5. Poll the returned job, save only after completion and return its authorized PDF link. Verify that the output belongs to the submitted job and contains the requested document when a preview is available. Compilation and publication of the downloadable artifact do not send or file it.

Use [Compilation contracts and recovery](references/compilation.md) for payloads and failure handling.
