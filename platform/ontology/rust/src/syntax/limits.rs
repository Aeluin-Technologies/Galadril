//! A lexical depth check bounds Pest recursion before it reads grammar rules.

use std::iter::Peekable;

use super::ast::Span;
use super::diagnostic::{ErrorCode, Failure};

const MAX_BYTES: usize = 1024 * 1024;
const MAX_NESTING: usize = 32;
const MAX_TOKENS: usize = 32_768;

type Bytes<'a> = Peekable<std::iter::Enumerate<std::str::Bytes<'a>>>;

pub(super) fn check(text: &str) -> Result<(), Failure> {
    if text.len() > MAX_BYTES {
        return Err(Failure::new(
            ErrorCode::SourceTooLarge,
            Span { start: 0, end: 0 },
        ));
    }
    let mut bytes = text.bytes().enumerate().peekable();
    let mut stack = [0; MAX_NESTING];
    let mut depth = 0usize;
    let mut generic = false;
    let mut tokens = 0usize;

    while let Some((offset, byte)) = bytes.next() {
        if skip_trivia(&mut bytes, byte, text)? {
            continue;
        }
        if !byte.is_ascii() {
            let width = text
                .get(offset..)
                .and_then(|s| s.chars().next())
                .map_or(0, char::len_utf8);
            return Err(Failure::syntax(
                Span {
                    start: offset,
                    end: offset + width,
                },
                Some("an ASCII token outside literals and comments"),
            ));
        }
        tokens += 1;
        if tokens > MAX_TOKENS {
            return Err(Failure::new(ErrorCode::TokenLimit, at(offset)));
        }
        match byte {
            b'"' | b'\'' => {
                if !end_literal(&mut bytes, byte) {
                    return Err(Failure::syntax(
                        eof(text),
                        Some("a closing quote"),
                    ));
                }
            },
            b'{' | b'(' | b'[' | b'<' if byte != b'<' || generic => {
                let Some(slot) = stack.get_mut(depth) else {
                    return Err(Failure::new(
                        ErrorCode::NestingLimit,
                        at(offset),
                    ));
                };
                *slot = byte;
                depth += 1;
            },
            b'}' | b')' | b']' | b'>' => {
                let top =
                    stack.get(..depth).and_then(|open| open.last()).copied();
                let open = match byte {
                    b'}' => b'{',
                    b')' => b'(',
                    b']' => b'[',
                    _ => b'<',
                };
                if top == Some(open) {
                    depth -= 1;
                } else if byte != b'>' {
                    return Err(Failure::syntax(
                        at(offset),
                        top.and_then(closing),
                    ));
                }
            },
            b if identifier_byte(b) => {
                let mut end = offset + 1;
                while let Some((index, _)) =
                    bytes.next_if(|&(_, b)| identifier_byte(b))
                {
                    end = index + 1;
                }
                generic = matches!(
                    text.get(offset..end),
                    Some("Option" | "List" | "Ref")
                );
                continue;
            },
            _ => {},
        }
        generic = false;
    }
    if let Some(&open) = stack.get(..depth).and_then(|open| open.last()) {
        return Err(Failure::syntax(eof(text), closing(open)));
    }
    Ok(())
}

fn skip_trivia(
    bytes: &mut Bytes<'_>,
    byte: u8,
    text: &str,
) -> Result<bool, Failure> {
    match byte {
        b' ' | b'\t' | b'\n' | b'\r' => Ok(true),
        b'/' if next_is(bytes, b'/') => {
            while bytes
                .next_if(|&(_, b)| !matches!(b, b'\r' | b'\n'))
                .is_some()
            {}
            Ok(true)
        },
        b'/' if next_is(bytes, b'*') => {
            if !end_comment(bytes) {
                return Err(Failure::syntax(eof(text), Some("a closing */")));
            }
            Ok(true)
        },
        _ => Ok(false),
    }
}

fn identifier_byte(byte: u8) -> bool {
    byte.is_ascii_alphanumeric() || byte == b'_'
}

fn end_comment(bytes: &mut Bytes<'_>) -> bool {
    while let Some((_, byte)) = bytes.next() {
        if byte == b'*' && next_is(bytes, b'/') {
            return true;
        }
    }
    false
}

fn end_literal(bytes: &mut Bytes<'_>, quote: u8) -> bool {
    while let Some((_, byte)) = bytes.next() {
        if byte == quote {
            return true;
        }
        if byte == b'\\' && bytes.next().is_none() {
            return false;
        }
    }
    false
}

fn next_is(bytes: &mut Bytes<'_>, expected: u8) -> bool {
    bytes.next_if(|&(_, byte)| byte == expected).is_some()
}

fn at(start: usize) -> Span {
    Span {
        start,
        end: start + 1,
    }
}

fn eof(text: &str) -> Span {
    Span {
        start: text.len(),
        end: text.len(),
    }
}

fn closing(open: u8) -> Option<&'static str> {
    match open {
        b'{' => Some("a closing }"),
        b'(' => Some("a closing )"),
        b'[' => Some("a closing ]"),
        b'<' => Some("a closing >"),
        _ => None,
    }
}
