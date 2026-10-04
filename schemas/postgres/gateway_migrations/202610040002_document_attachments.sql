-- Adds PDF references without storing document bytes or signed URLs.

ALTER TABLE conversation_message_attachments
DROP CONSTRAINT conversation_message_attachments_kind_check;
ALTER TABLE conversation_message_attachments
ADD CONSTRAINT conversation_message_attachments_kind_check
CHECK (kind IN ('image', 'audio', 'document'));
