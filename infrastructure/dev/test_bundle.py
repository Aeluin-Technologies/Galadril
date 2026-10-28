"""Bundle preparation never extracts paths or publishes unverified resources."""

from __future__ import annotations

import gzip
import hashlib
import io
import struct
import tarfile
import tempfile
import unittest
from pathlib import Path

from infrastructure.dev.galadril_dev.tex_bundle import BundleError, pack


def archive(entries: list[tuple[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        for name, content in entries:
            metadata = tarfile.TarInfo(name)
            metadata.size = len(content)
            bundle.addfile(metadata, io.BytesIO(content))
    return output.getvalue()


class BundleTest(unittest.TestCase):
    def test_complete_resources_are_packed_deterministically_with_valid_hashes(
        self,
    ) -> None:
        source = archive(
            [
                ("article.cls", b"public class"),
                ("font.otf", b"public font"),
                ("nested/package.sty", b"public package"),
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = pack(
                io.BytesIO(source),
                root / "one",
                hashlib.sha256(source).hexdigest(),
            )
            second = pack(
                io.BytesIO(source),
                root / "two",
                hashlib.sha256(source).hexdigest(),
            )
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(first.files, 3)
            content = (root / "one/bundle.ttb").read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), first.sha256)
            header = struct.unpack("<14sIQII32sI", content[:70])
            self.assertEqual(header[0], b"tectonicbundle")
            self.assertEqual(header[1], 1)
            index = gzip.decompress(content[header[2] :])
            self.assertEqual(hashlib.sha256(index).digest(), header[5])
            self.assertIn(b"nested/package.sty", index)
            self.assertTrue((root / "one/bundle.sha256").is_file())
            self.assertFalse((root / "one/bundle.ttb.part").exists())

    def test_wrong_source_digest_never_publishes_bundle(self) -> None:
        source = archive([("article.cls", b"public class")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(BundleError):
                pack(io.BytesIO(source), root, "0" * 64)
            self.assertFalse((root / "bundle.ttb").exists())
            self.assertFalse((root / "bundle.sha256").exists())

    def test_traversal_links_and_duplicate_files_are_rejected(self) -> None:
        for name in ("../escape", "/absolute", "line\nbreak"):
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                source = archive([(name, b"invalid")])
                with self.assertRaises(BundleError):
                    pack(
                        io.BytesIO(source),
                        Path(temporary),
                        hashlib.sha256(source).hexdigest(),
                    )
        source = archive([("same.cls", b"first"), ("same.cls", b"second")])
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaises(BundleError),
        ):
            pack(
                io.BytesIO(source),
                Path(temporary),
                hashlib.sha256(source).hexdigest(),
            )

    def test_failed_refresh_preserves_existing_complete_bundle(self) -> None:
        source = archive([("article.cls", b"public class")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = pack(
                io.BytesIO(source), root, hashlib.sha256(source).hexdigest()
            )
            before = (root / "bundle.ttb").read_bytes()
            with self.assertRaises(BundleError):
                pack(io.BytesIO(source), root, "0" * 64)
            self.assertEqual((root / "bundle.ttb").read_bytes(), before)
            self.assertEqual(
                (root / "bundle.sha256").read_text().strip(), result.sha256
            )


if __name__ == "__main__":
    unittest.main()
