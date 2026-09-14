from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from checkpoint_align.dwarf_source import AddressRange, Anchor, AnchorCatalog
from checkpoint_align.qemu_occurrence.collect import decode_trace, select_entries


def catalog(anchors: tuple[Anchor, ...]) -> AnchorCatalog:
    return AnchorCatalog("fixture", "a" * 64, None, "fixture", None, "RISC-V", 0x1000, 0x100, anchors, (), (), (), {}, {}, {})


class QemuOccurrenceTest(unittest.TestCase):
    def test_decode_rejects_truncation_and_unknown_watch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.u32"
            path.write_bytes(b"abc")
            with self.assertRaisesRegex(RuntimeError, "truncated"):
                decode_trace(path, {}, 10)
            path.write_bytes(struct.pack("<I", 7))
            with self.assertRaisesRegex(RuntimeError, "illegal watch ID"):
                decode_trace(path, {}, 10)

    def test_watchlist_rejects_multiple_anchors_at_same_pc(self) -> None:
        first = Anchor("a", "function", "a", "a", None, (AddressRange(0x1000, 0x1010),), "M", "fixture")
        second = Anchor("b", "symbol", "b", "b", None, (AddressRange(0x1000, 0x1010),), "L", "fixture")
        with self.assertRaisesRegex(ValueError, "multiple catalog anchors"):
            select_entries(catalog((first, second)), {"a"}, set())


if __name__ == "__main__":
    unittest.main()
