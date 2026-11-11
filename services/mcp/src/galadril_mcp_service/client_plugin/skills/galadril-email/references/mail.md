# Mailbox and draft contracts

Use the account ID and message ID returned by the same accessible mailbox
when opening a message or thread. Search the contact directory by an exact
address if known. Search messages with distinctive terms from the subject
or sender and confirm the matching sender and date on the message itself.
If a thread contains forwards, identify the actual recipient of the reply.

For a reply, use `content.reply_source = {"account":"ACCOUNT","message":"MESSAGE"}`
in `save_email_draft`. The outer `request.account` is the authorized sending
mailbox. Choose it from the available accounts and confirm ambiguity with
the user. Use `draft` and its current `revision` to edit an existing draft.
To add a generated PDF, give `file_ids` from the Galadril save result.

After changing recipients, body or attachments, review the updated revision
before sending. If delivery is pending or uncertain, check the draft or
proposal status before attempting another send. An approval link means the
user still needs to act. Never turn an uncertain delivery into a fresh
draft and second send.
