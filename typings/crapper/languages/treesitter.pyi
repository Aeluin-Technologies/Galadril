"""Type contract for the pinned, prepackaged tree-sitter grammars."""

from collections.abc import Iterator

from tree_sitter import Node, Parser

def parser_for(language: str) -> Parser: ...
def descendants(node: Node) -> Iterator[Node]: ...
