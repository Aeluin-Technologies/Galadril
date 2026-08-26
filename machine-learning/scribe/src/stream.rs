//! Streaming response types and reasoning-tag parsing.

/// Distinct streaming tokens categorized by inferential layer types.
#[derive(Debug, Clone)]
pub enum ScribeStreamChunk {
    Reasoning(String),
    Content(String),
}

/// Clean flat state machine processing network stream boundaries cleanly
/// without deep brackets.
pub(crate) struct TokenStreamParser {
    buffer: String,
    in_reasoning: bool,
}

impl TokenStreamParser {
    const END_TAG: &'static str = "</reasoning>";
    const START_TAG: &'static str = "<reasoning>";

    pub(crate) fn new() -> Self {
        Self {
            buffer: String::new(),
            in_reasoning: false,
        }
    }

    fn get_partial_match_len(&self, tag: &str) -> usize {
        for len in (1..tag.len()).rev() {
            if let Some(prefix) = tag.get(..len) &&
                self.buffer.ends_with(prefix)
            {
                return len;
            }
        }
        0
    }

    fn consume(
        &mut self,
        content_end: usize,
        remainder_start: usize,
    ) -> String {
        let source = std::mem::take(&mut self.buffer);
        let mut content = String::with_capacity(content_end);
        self.buffer
            .reserve(source.len().saturating_sub(remainder_start));
        // Model fragments may contain multibyte text, so classify character
        // boundaries by byte offset instead of using panic-prone string
        // slices.
        for (offset, character) in source.char_indices() {
            if offset < content_end {
                content.push(character);
            } else if offset >= remainder_start {
                self.buffer.push(character);
            }
        }
        content
    }

    pub(crate) fn advance(
        &mut self,
        token: &str,
        output: &mut Vec<ScribeStreamChunk>,
    ) {
        self.buffer.push_str(token);

        loop {
            if !self.in_reasoning {
                if let Some(idx) = self.buffer.find(Self::START_TAG) {
                    let content = self.consume(
                        idx,
                        idx.saturating_add(Self::START_TAG.len()),
                    );
                    if !content.is_empty() {
                        output.push(ScribeStreamChunk::Content(content));
                    }
                    self.in_reasoning = true;
                    continue;
                }

                let partial = self.get_partial_match_len(Self::START_TAG);
                let flush_len = self.buffer.len() - partial;
                if flush_len > 0 {
                    let content = self.consume(flush_len, flush_len);
                    output.push(ScribeStreamChunk::Content(content));
                }
            } else {
                if let Some(idx) = self.buffer.find(Self::END_TAG) {
                    let reasoning = self
                        .consume(idx, idx.saturating_add(Self::END_TAG.len()));
                    if !reasoning.is_empty() {
                        output.push(ScribeStreamChunk::Reasoning(reasoning));
                    }
                    self.in_reasoning = false;
                    continue;
                }

                let partial = self.get_partial_match_len(Self::END_TAG);
                let flush_len = self.buffer.len() - partial;
                if flush_len > 0 {
                    let reasoning = self.consume(flush_len, flush_len);
                    output.push(ScribeStreamChunk::Reasoning(reasoning));
                }
            }
            break;
        }
    }

    pub(crate) fn flush(self) -> Option<ScribeStreamChunk> {
        if self.buffer.is_empty() {
            return None;
        }
        if self.in_reasoning {
            Some(ScribeStreamChunk::Reasoning(self.buffer))
        } else {
            Some(ScribeStreamChunk::Content(self.buffer))
        }
    }
}
