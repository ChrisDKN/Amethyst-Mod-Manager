"""
gpak — read GPAK archives (Mewgenics / The End Is Nigh style).

Format (ZenHAX): file count, then per-file (name length, name, stored size),
then file data sequentially. File data may be zlib-compressed.
"""

from gpak.reader import GpakEntry, GpakReader, list_gpak, extract_gpak
from gpak.writer import pack_gpak

__all__ = ["GpakEntry", "GpakReader", "list_gpak", "extract_gpak", "pack_gpak"]
