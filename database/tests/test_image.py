"""Verifies both executable platforms and retained supply-chain attestations."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

IMAGE = Path(__file__).absolute().parents[1] / "image"


def document(digest: str) -> dict[str, object]:
    algorithm, value = digest.split(":", 1)
    return cast(
        dict[str, object],
        json.loads((IMAGE / "blobs" / algorithm / value).read_bytes()),
    )


def manifests(index: dict[str, object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for descriptor in cast(list[dict[str, object]], index["manifests"]):
        content = document(cast(str, descriptor["digest"]))
        if "manifests" in content:
            result.extend(manifests(content))
        else:
            result.append(content)
    return result


class DatabaseImageTest(unittest.TestCase):
    def test_both_platforms_keep_sbom_and_provenance(self) -> None:
        index = cast(
            dict[str, object], json.loads((IMAGE / "index.json").read_bytes())
        )
        self.assertEqual(len(cast(list[object], index["manifests"])), 1)
        platforms: set[tuple[str, str]] = set()
        sboms = 0
        provenance = 0
        for manifest in manifests(index):
            config = cast(dict[str, object], manifest["config"])
            configuration = document(cast(str, config["digest"]))
            if configuration.get("os") == "linux":
                platforms.add(
                    ("linux", cast(str, configuration["architecture"]))
                )
            for layer in cast(list[dict[str, object]], manifest["layers"]):
                if layer["mediaType"] != "application/vnd.in-toto+json":
                    continue
                statement = document(cast(str, layer["digest"]))
                predicate = cast(str, statement["predicateType"])
                sboms += predicate == "https://spdx.dev/Document"
                provenance += predicate.startswith(
                    "https://slsa.dev/provenance/"
                )
        self.assertEqual(platforms, {("linux", "amd64"), ("linux", "arm64")})
        self.assertEqual(sboms, 2)
        self.assertEqual(provenance, 2)


if __name__ == "__main__":
    unittest.main()
