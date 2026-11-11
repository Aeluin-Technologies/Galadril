# Compilation contracts and recovery

## Choose the file to write

Read the template with no path. The server reports the exact replacement
path in `document_source_target`. Compare it to the published
`entrypoint`. Read that file and any dependencies actually listed in
`files`; read the corresponding includes to learn which definitions
and document environment the replacement needs. Only declared fields
outside `server_bound_variables` may be supplied in `document.variables`.

## Calls in order

The IDs below are descriptive placeholders. Replace them with values
returned by Galadril. Use one project throughout.

- `document_templates`: arguments `{"request":{"action":"list"},"project_id":"PROJECT"}`.
- `document_templates`: read the returned template version with
  `{"request":{"action":"read","template_id":"TEMPLATE","version":"VERSION","path":"DISCOVERED_PATH"},"project_id":"PROJECT"}`.
  Read the published entrypoint and the relevant class or supporting asset.
  Respect returned offsets when a file is paginated.
- `document_case_status`: `{"template_id":"TEMPLATE","project_id":"PROJECT"}`.
  The response reports `missing_project_fields`, `profile_ready` and
  `can_update`. `profile_ready` is a readiness check. The compiler binds
  the actual values on the server.
- If the user confirms the missing public case details and `can_update`
  is true, use `manage_document_templates` with
  `{"request":{"action":"document_case","template_id":"TEMPLATE","fields":{"court_name":"Confirmed court"}},"project_id":"PROJECT"}`.
  Supply the confirmed values under the exact keys returned by preflight.
  An unknown field stays unresolved. Ask the project owner to update it
  when the current user lacks permission.
- `compile_document`: put `template_id`, `version`, `idempotency_key`
  and `document` under `request`. Put the finished TeX in `document.source`.
  Keep `project_id` outside `request`. Supply only declared user-controlled variables. Keep server-bound values out of the request.
- `document_job_status`: pass the returned `job_id` and `project_id`.
  Continue while queued or running. On completed, call `document_job`
  with `{"request":{"action":"save","job_id":"JOB","name":"courrier-au-greffe.pdf"},"project_id":"PROJECT"}`.

## Recover without substituting the demo

- **Profile incomplete** — ask the user to update Compte, then check again.
  Do not collect their private profile in the conversation.
- **Project or template denied** — check that the intended project, template
  ID and version came from the same authorized listing. Report the blocked
  operation if the tool still denies it.
- **Unknown control sequence** — reopen the exact class version and use a
  documented command. Check braces and TeX escaping before recompiling.
- **Failed job or rejected source** — use the diagnostics available to the
  caller and correct the specific issue. A revised source needs a new
  idempotency key. Poll an existing job after an ambiguous timeout.
- **PDF contains sample text** — check that the compile request carried the
  full requested TeX in `document.source`. Compile the corrected request
  and save the new job's PDF. A successful demo is evidence of compiler
  availability only.

Before delivering, confirm the saved artifact belongs to the completed job
created from the requested source. If content inspection is available,
check the document type, subject, dossier and absence of sample text.
Return the authorized link without reproducing vault values. Filing or
sending is a separate user action.
