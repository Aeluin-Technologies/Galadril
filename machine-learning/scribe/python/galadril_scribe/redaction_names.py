"""Shared name and contact patterns for deterministic and learned redaction."""

import re2

EMAIL_PATTERN = r"\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9](?:[A-Z0-9.-]*[A-Z0-9])?\.[A-Z]{2,63}\b"
PHONE_PATTERN = r"(?:\b0[1-9]|\+33[ .-]?[1-9])(?:[ .-]?[0-9]{2}){4}\b"

_WORDS = re2.compile(r"[\p{L}][\p{L}\p{M}'’\-]*")
_TITLE = re2.compile(
    r"(?i)^(Monsieur|Madame|M\.|Mme|Me|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Pr\.?|Prof\.?)[ \t]+"
)


def initials(name: str) -> str:
    if len(name) > 512:
        raise ValueError("Name match exceeds limit")
    title = _TITLE.match(name)
    prefix = title.group(1) + " " if title is not None else ""
    words = _WORDS.findall(name[title.end() if title is not None else 0 :])
    if not 1 <= len(words) <= 8:
        raise ValueError("Name match requires review")
    result = prefix + " ".join(word[0].upper() + "." for word in words)
    if len(result) > 64:
        raise ValueError("Public initials exceed limit")
    return result


def initial_alias(name: str) -> str | None:
    """Only abbreviate a supplied given name, without guessing a surname."""
    title = _TITLE.match(name)
    words = name[title.end() if title is not None else 0 :].split()
    if not 2 <= len(words) <= 8 or not all(
        _WORDS.fullmatch(word) for word in words
    ):
        return None
    gap = r"[ \t\x{00A0}\x{202F}]+"
    return gap.join(
        (re2.escape(words[0][0] + "."), *map(re2.escape, words[1:]))
    )
