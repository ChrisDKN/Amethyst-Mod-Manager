"""
nif_xml.py
Spec-driven block walker for NIFs that carry no per-block size table.

From version 20.2.0.5 the header lists every block's byte size, so
:mod:`Utils.nif_reader` can seek straight to the blocks it cares about and
skip the rest blind. Oblivion-era files (20.0.0.5 and earlier) have no such
table: the only way past a block is to know its exact layout. Oblivion alone
uses 70+ block types - havok collision, particle systems, controller and
interpolator graphs - so hand-writing skippers is neither small nor safe, and
a single wrong field silently desyncs every block after it.

Instead this module interprets NifTools' ``nif.xml`` (vendored under
``Utils/nifxml/``), the same description NifSkope is generated from. Block
layouts, field types, array lengths and the version/user-version conditions
that gate each field all come from the spec, so the walk is exact for every
block type and every pre-Skyrim NIF version.

    spec = load_spec()
    for idx, offset, size, values in spec.walk(data, header, want={"NiTriShape"}):
        ...

``walk`` reads every scalar (later fields' conditions and array lengths depend
on them) but only materialises array *contents* for the block types in *want*,
which keeps the common case - stepping over collision and animation data to
reach geometry - from paying for data nobody reads.

nif.xml is BSD-licensed by the NifTools team; ``Utils/nifxml/nif.xml`` is an
unmodified copy.
"""

from __future__ import annotations

import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

__all__ = ["NifSpecError", "NifSpec", "load_spec", "spec_path"]

_XML_PATH = Path(__file__).resolve().parent / "nifxml" / "nif.xml"

# Sizes of the primitives nif.xml declares but does not measure. Flags is a
# ushort, not a 32-bit field; getting that wrong shifts every following block.
_FIXED_PRIMITIVES = {
    "byte": 1, "char": 1,
    "short": 2, "ushort": 2, "Flags": 2,
    "int": 4, "uint": 4, "ulittle32": 4, "float": 4,
    "Ref": 4, "Ptr": 4, "StringOffset": 4, "StringIndex": 4,
    "BlockTypeIndex": 2, "FileVersion": 4,
}

_STRUCT_CODE = {
    "byte": "B", "char": "c", "short": "h", "ushort": "H", "Flags": "H",
    "int": "i", "uint": "I", "ulittle32": "I", "float": "f",
    "Ref": "i", "Ptr": "i", "StringOffset": "i", "StringIndex": "i",
    "BlockTypeIndex": "H", "FileVersion": "I",
}

# bool widened from a 32-bit word to a single byte in 4.1.0.1.
_BOOL_BYTE_FROM = 0x04010001


class NifSpecError(Exception):
    """Raised when the spec cannot be applied to a file."""


def _ver_to_int(text: str | None) -> int | None:
    """Turn a dotted spec version ('20.0.0.5') into its packed integer."""
    if not text:
        return None
    parts = text.strip().split(".")
    if not all(p.isdigit() for p in parts) or not 1 <= len(parts) <= 4:
        return None
    parts += ["0"] * (4 - len(parts))
    v = 0
    for p in parts:
        n = int(p)
        if n > 0xFF:
            return None
        v = (v << 8) | n
    return v


# ---------------------------------------------------------------------------
# Condition expressions
#
# nif.xml gates fields with expressions over previously-read fields and the
# version triple, e.g. "Has Texture Transform", "Texture Count >= 10",
# "(Has Faces) && (Num Strips != 0)", "(Flags & 2)!=0". Field names contain
# spaces and even '?', so the tokeniser matches identifiers greedily and the
# parser resolves them against the current scope.
# ---------------------------------------------------------------------------

_OPERATORS = [
    "&&", "||", "==", "!=", "<=", ">=", "<", ">", "&", "|", "!", "(", ")",
    "+", "-", "*", "/",
]


def _tokenise(src: str) -> list[tuple[str, str]]:
    """Split an expression into ('op'|'num'|'ver'|'name', text) tokens."""
    out: list[tuple[str, str]] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch.isspace():
            i += 1
            continue
        for op in _OPERATORS:
            if src.startswith(op, i):
                out.append(("op", op))
                i += len(op)
                break
        else:
            j = i
            while j < n and not src[j].isspace() and not any(
                    src.startswith(op, j) for op in _OPERATORS):
                j += 1
            word = src[i:j]
            # A dotted literal is a version; a bare run of digits a number.
            if word.replace(".", "").isdigit() and "." in word:
                out.append(("ver", word))
            elif word.isdigit():
                out.append(("num", word))
            else:
                # Identifiers may contain spaces and trailing digits - "User
                # Version 2", "Has Unknown Floats 3", "Unknown Short 1". Absorb
                # every following word up to an operator: a bare number only
                # ever follows an operator, never another word of a name.
                k = j
                while k < n:
                    while k < n and src[k] == " ":
                        k += 1
                    if k >= n or any(src.startswith(op, k) for op in _OPERATORS):
                        break
                    m = k
                    while m < n and not src[m].isspace() and not any(
                            src.startswith(op, m) for op in _OPERATORS):
                        m += 1
                    word = f"{word} {src[k:m]}"
                    k = m
                    j = m
                out.append(("name", word))
            i = j
    return out


class _Expr:
    """Precedence-climbing evaluator over a tokenised nif.xml condition."""

    _PRECEDENCE = {
        "||": 1, "&&": 2, "|": 3, "&": 4,
        "==": 5, "!=": 5, "<": 6, ">": 6, "<=": 6, ">=": 6,
        "+": 7, "-": 7, "*": 8, "/": 8,
    }

    def __init__(self, src: str):
        self.src = src
        self.tokens = _tokenise(src)
        # Field names this expression reads, so the walk knows which values
        # it must keep even when it is otherwise discarding array contents.
        self.names = {text for kind, text in self.tokens if kind == "name"}

    def evaluate(self, scope) -> int:
        """Evaluate against *scope*, a name -> int lookup.

        Parse position is threaded through the calls rather than stored on
        self: one _Expr is shared by every block of a type, and the viewer
        parses meshes off the UI thread, so instance state would let two
        walks corrupt each other's parse.
        """
        try:
            val, _ = self._parse(0, 0, scope)
        except (IndexError, ValueError, ZeroDivisionError):
            # An expression we cannot resolve must not silently read as
            # "field present" - that would desync the walk.
            return 0
        return val

    def _parse(self, pos: int, min_prec: int, scope) -> tuple[int, int]:
        left, pos = self._parse_unary(pos, scope)
        while pos < len(self.tokens):
            kind, text = self.tokens[pos]
            if kind != "op":
                break
            prec = self._PRECEDENCE.get(text)
            if prec is None or prec < min_prec:
                break
            right, pos = self._parse(pos + 1, prec + 1, scope)
            left = self._apply(text, left, right)
        return left, pos

    def _parse_unary(self, pos: int, scope) -> tuple[int, int]:
        if pos >= len(self.tokens):
            raise ValueError("empty expression")
        tok = self.tokens[pos]
        if tok == ("op", "!"):
            val, pos = self._parse_unary(pos + 1, scope)
            return (0 if val else 1), pos
        if tok == ("op", "-"):
            val, pos = self._parse_unary(pos + 1, scope)
            return -val, pos
        if tok == ("op", "("):
            val, pos = self._parse(pos + 1, 0, scope)
            if pos < len(self.tokens) and self.tokens[pos] == ("op", ")"):
                pos += 1
            return val, pos
        kind, text = tok
        pos += 1
        if kind == "num":
            return int(text), pos
        if kind == "ver":
            v = _ver_to_int(text)
            if v is None:
                raise ValueError(f"bad version literal {text}")
            return v, pos
        if kind == "name":
            return scope(text), pos
        raise ValueError(f"unexpected token {tok}")

    @staticmethod
    def _apply(op: str, a: int, b: int) -> int:
        if op == "&&":
            return 1 if (a and b) else 0
        if op == "||":
            return 1 if (a or b) else 0
        if op == "==":
            return 1 if a == b else 0
        if op == "!=":
            return 1 if a != b else 0
        if op == "<":
            return 1 if a < b else 0
        if op == ">":
            return 1 if a > b else 0
        if op == "<=":
            return 1 if a <= b else 0
        if op == ">=":
            return 1 if a >= b else 0
        if op == "&":
            return int(a) & int(b)
        if op == "|":
            return int(a) | int(b)
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a // b if b else 0
        raise ValueError(f"unknown operator {op}")


# ---------------------------------------------------------------------------
# Spec model
# ---------------------------------------------------------------------------

@dataclass
class _Field:
    name: str
    type: str
    template: str | None = None
    ver1: int | None = None
    ver2: int | None = None
    userver: int | None = None
    cond: _Expr | None = None
    vercond: _Expr | None = None
    arr1: _Expr | None = None
    arr2: _Expr | None = None
    arr1_const: int | None = None
    arr2_const: int | None = None
    # Raw arr2 text. When it names an array field the rows are ragged and
    # row *i* is arr2_value[i] long (triangle strips are the common case).
    arr2_src: str | None = None
    # Value passed down as ARG to the nested compound (a field name here).
    arg: str | None = None


class NifSpec:
    """Parsed nif.xml: block layouts plus the machinery to walk them."""

    def __init__(self, path: Path | str = _XML_PATH):
        self.path = Path(path)
        try:
            root = ET.parse(self.path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise NifSpecError(f"cannot read {self.path}: {exc}") from exc

        self._compounds: dict[str, list[_Field]] = {}
        self._objects: dict[str, list[_Field]] = {}
        self._inherit: dict[str, str | None] = {}
        self._storage: dict[str, str] = {}

        for el in root.findall("enum") + root.findall("bitflags"):
            name, storage = el.get("name"), el.get("storage")
            if name and storage:
                self._storage[name] = storage
        for el in root.findall("compound"):
            name = el.get("name")
            if name:
                self._compounds[name] = [self._field(a) for a in el.findall("add")]
        for el in root.findall("niobject"):
            name = el.get("name")
            if name:
                self._objects[name] = [self._field(a) for a in el.findall("add")]
                self._inherit[name] = el.get("inherit")

        # (type, version, user_version, bs_version) -> resolved field list
        self._chain_cache: dict[str, list[_Field]] = {}
        self._fixed_cache: dict[tuple, int | None] = {}
        self._bulk_cache: dict[tuple, str | None] = {}
        self._refs_cache: dict[int, set[str]] = {}

    # -- construction helpers ------------------------------------------

    def _field(self, el) -> _Field:
        cond = el.get("cond")
        vercond = el.get("vercond")
        arr1 = el.get("arr1")
        arr2 = el.get("arr2")
        userver = el.get("userver")
        f = _Field(
            name=el.get("name") or "",
            type=el.get("type") or "",
            template=el.get("template"),
            ver1=_ver_to_int(el.get("ver1")),
            ver2=_ver_to_int(el.get("ver2")),
            userver=int(userver) if userver and userver.isdigit() else None,
            cond=_Expr(cond) if cond else None,
            vercond=_Expr(vercond) if vercond else None,
            arg=el.get("arg"),
        )
        # Array lengths are usually a bare field name, sometimes a literal.
        if arr1:
            if arr1.isdigit():
                f.arr1_const = int(arr1)
            else:
                f.arr1 = _Expr(arr1)
        if arr2:
            f.arr2_src = arr2
            if arr2.isdigit():
                f.arr2_const = int(arr2)
            else:
                f.arr2 = _Expr(arr2)
        return f

    def has_object(self, name: str) -> bool:
        return name in self._objects

    def _chain(self, type_name: str) -> list[_Field]:
        """Field list for *type_name*, base classes first."""
        got = self._chain_cache.get(type_name)
        if got is not None:
            return got
        fields: list[_Field] = []
        seen: set[str] = set()
        stack: list[str] = []
        cur: str | None = type_name
        while cur and cur in self._objects and cur not in seen:
            seen.add(cur)
            stack.append(cur)
            cur = self._inherit.get(cur)
        for name in reversed(stack):
            fields.extend(self._objects[name])
        self._chain_cache[type_name] = fields
        return fields

    # -- sizing --------------------------------------------------------

    def _elem_size(self, type_name: str, key: tuple,
                   tmpl: str | None = None) -> int | None:
        """Byte size of *type_name* at this version, or None if variable."""
        ck = (type_name, key, tmpl)
        got = self._fixed_cache.get(ck, ...)
        if got is not ...:
            return got                              # type: ignore[return-value]
        size = self._elem_size_uncached(type_name, key, tmpl)
        self._fixed_cache[ck] = size
        return size

    def _elem_size_uncached(self, type_name: str, key: tuple,
                            tmpl: str | None) -> int | None:
        version = key[0]
        if type_name == "TEMPLATE":
            if not tmpl:
                return None
            type_name = tmpl
        type_name = self._storage.get(type_name, type_name)
        if type_name == "bool":
            return 1 if version >= _BOOL_BYTE_FROM else 4
        if type_name in _FIXED_PRIMITIVES:
            return _FIXED_PRIMITIVES[type_name]
        fields = self._compounds.get(type_name)
        if fields is None:
            return None
        total = 0
        for f in fields:
            # Anything gated on a runtime value, or any array, makes the
            # compound variable-length.
            if f.cond is not None or f.arr1 is not None or f.arr1_const:
                return None
            if not self._static_ok(f, key):
                continue
            sub = self._elem_size(f.type, key, f.template or tmpl)
            if sub is None:
                return None
            total += sub
        return total

    def _bulk_format(self, type_name: str, key: tuple,
                     tmpl: str | None = None) -> str | None:
        """struct code for a numeric compound, so arrays unpack in one call."""
        ck = (type_name, key, tmpl)
        got = self._bulk_cache.get(ck, ...)
        if got is not ...:
            return got                              # type: ignore[return-value]
        fmt = self._bulk_format_uncached(type_name, key, tmpl)
        self._bulk_cache[ck] = fmt
        return fmt

    def _bulk_format_uncached(self, type_name: str, key: tuple,
                              tmpl: str | None) -> str | None:
        if type_name == "TEMPLATE":
            if not tmpl:
                return None
            type_name = tmpl
        resolved = self._storage.get(type_name, type_name)
        if resolved in _STRUCT_CODE:
            return _STRUCT_CODE[resolved]
        fields = self._compounds.get(resolved)
        if fields is None:
            return None
        out = ""
        for f in fields:
            if f.cond is not None or f.arr1 is not None or f.arr1_const:
                return None
            if not self._static_ok(f, key):
                continue
            sub = self._bulk_format(f.type, key, f.template or tmpl)
            if sub is None:
                return None
            out += sub
        return out or None

    # -- conditions ----------------------------------------------------

    @staticmethod
    def _version_ok(f: _Field, key: tuple) -> bool:
        version, user_version, bs_version = key
        if f.ver1 is not None and version < f.ver1:
            return False
        if f.ver2 is not None and version > f.ver2:
            return False
        if f.userver is not None and user_version != f.userver:
            return False
        return True

    @staticmethod
    def _version_scope(key: tuple):
        """Scope that knows the version triple and nothing else.

        Every vercond in nif.xml is a pure version test, so sizing can settle
        them without having read any fields.
        """
        version, user_version, bs_version = key

        def resolve(name: str) -> int:
            if name == "Version":
                return version
            if name == "User Version":
                return user_version
            if name in ("User Version 2", "BS Header\\BS Version"):
                return bs_version
            return 0

        return resolve

    def _static_ok(self, f: _Field, key: tuple) -> bool:
        """Version gates including vercond - used when sizing a compound.

        HavokColFilter and HavokMaterial declare the same field three times,
        one per game, separated only by vercond. Ignoring vercond here counts
        all three and mis-sizes every havok collision block.
        """
        if not self._version_ok(f, key):
            return False
        if f.vercond is not None and not f.vercond.evaluate(
                self._version_scope(key)):
            return False
        return True

    def _referenced(self, fields: list[_Field]) -> set[str]:
        """Field names some other field in this list reads.

        Triangle strips are the motivating case: 'Points' is a ragged array
        whose row lengths live in the 'Strip Lengths' array. Dropping that
        array because the caller did not ask for it would size every row at
        zero and desync the rest of the file.
        """
        ck = id(fields)
        got = self._refs_cache.get(ck)
        if got is not None:
            return got
        names: set[str] = set()
        for f in fields:
            for expr in (f.cond, f.vercond, f.arr1, f.arr2):
                if expr is not None:
                    names |= expr.names
            if f.arr2_src:
                names.add(f.arr2_src)
            if f.arg:
                names.add(f.arg)
        self._refs_cache[ck] = names
        return names

    def _is_a(self, type_name: str, base: str) -> bool:
        """True when *type_name* is *base* or inherits from it."""
        cur: str | None = type_name
        seen: set[str] = set()
        while cur and cur not in seen:
            if cur == base:
                return True
            seen.add(cur)
            cur = self._inherit.get(cur)
        return False

    def _scope_fn(self, values: dict, key: tuple, arg: int,
                  btype: str | None = None):
        version, user_version, bs_version = key

        def resolve(name: str) -> int:
            if name == "Version":
                return version
            if name == "User Version":
                return user_version
            if name in ("User Version 2", "BS Header\\BS Version"):
                return bs_version
            if name == "ARG":
                return arg
            # A bare block-type name asks "is the block being read one of
            # these?" - e.g. NiGeometryData gates fields on '!NiPSysData'.
            if name in self._objects:
                return 1 if btype and self._is_a(btype, name) else 0
            got = values.get(name, 0)
            if isinstance(got, bool):
                return int(got)
            if isinstance(got, (int, float)):
                return int(got)
            # A ref/string/array in a numeric position: treat as present.
            return 1 if got else 0

        return resolve

    # -- reading -------------------------------------------------------

    def _read_value(self, data: bytes, pos: int, type_name: str, key: tuple,
                    capture: bool, arg: int, tmpl: str | None = None,
                    btype: str | None = None) -> tuple[object, int]:
        """Read one value of *type_name*; return (value, new_pos)."""
        version = key[0]
        if type_name == "TEMPLATE":
            if not tmpl:
                raise NifSpecError("TEMPLATE outside a templated compound")
            type_name = tmpl
        resolved = self._storage.get(type_name, type_name)

        if resolved == "bool":
            if version >= _BOOL_BYTE_FROM:
                return data[pos], pos + 1
            return struct.unpack_from("<I", data, pos)[0], pos + 4
        code = _STRUCT_CODE.get(resolved)
        if code is not None:
            size = _FIXED_PRIMITIVES[resolved]
            if pos + size > len(data):
                raise NifSpecError("read past end of block")
            if resolved == "char":
                return data[pos], pos + 1
            return struct.unpack_from("<" + code, data, pos)[0], pos + size
        if resolved in ("HeaderString", "LineString"):
            end = data.find(b"\n", pos)
            if end < 0:
                raise NifSpecError("unterminated header string")
            return data[pos:end].decode("latin-1"), end + 1

        fields = self._compounds.get(resolved)
        if fields is None:
            raise NifSpecError(f"unknown type {type_name!r}")
        return self._read_fields(data, pos, fields, key, capture, arg, tmpl,
                                 btype)

    def _read_fields(self, data: bytes, pos: int, fields: list[_Field],
                     key: tuple, capture: bool, arg: int,
                     tmpl: str | None = None,
                     btype: str | None = None) -> tuple[dict, int]:
        values: dict = {}
        scope = self._scope_fn(values, key, arg, btype)
        needed = self._referenced(fields)
        for f in fields:
            if not self._version_ok(f, key):
                continue
            if f.vercond is not None and not f.vercond.evaluate(scope):
                continue
            if f.cond is not None and not f.cond.evaluate(scope):
                continue

            # A templated field names its own template; otherwise the
            # enclosing compound's template flows down unchanged.
            sub_tmpl = f.template or tmpl
            if sub_tmpl == "TEMPLATE":
                sub_tmpl = tmpl
            sub_arg = scope(f.arg) if f.arg else arg

            count = f.arr1_const
            if count is None and f.arr1 is not None:
                count = f.arr1.evaluate(scope)
            if count is None:
                val, pos = self._read_value(
                    data, pos, f.type, key, capture, sub_arg, sub_tmpl,
                    btype)
                values[f.name] = val
                continue

            if count < 0 or count > len(data):
                raise NifSpecError(f"implausible array length {count}")

            # An array another field measures itself against must survive
            # even when this block's contents are being discarded.
            keep = capture or f.name in needed

            if f.arr2_const is not None or f.arr2 is not None:
                # Ragged when arr2 names an array: row i has its own length.
                per_row = values.get(f.arr2_src) if f.arr2_src else None
                if not isinstance(per_row, (list, tuple)):
                    per_row = None
                    flat = (f.arr2_const if f.arr2_const is not None
                            else f.arr2.evaluate(scope))   # type: ignore[union-attr]
                rows = []
                for r in range(count):
                    length = int(per_row[r]) if per_row is not None else flat
                    row, pos = self._read_array(
                        data, pos, f, key, length, keep, sub_arg,
                        sub_tmpl, btype)
                    rows.append(row)
                values[f.name] = rows if keep else None
                continue

            val, pos = self._read_array(
                data, pos, f, key, count, keep, sub_arg, sub_tmpl, btype)
            values[f.name] = val
        return values, pos

    def _read_array(self, data: bytes, pos: int, f: _Field, key: tuple,
                    count: int, capture: bool, arg: int,
                    tmpl: str | None = None, btype: str | None = None):
        """Read (or step over) *count* elements of f.type."""
        resolved = f.type
        if resolved == "TEMPLATE" and tmpl:
            resolved = tmpl
        resolved = self._storage.get(resolved, resolved)

        # char arrays are strings - always materialised, they are cheap and
        # every block name and texture path is one.
        if resolved == "char":
            if pos + count > len(data):
                raise NifSpecError("string past end of block")
            return data[pos:pos + count].decode("latin-1"), pos + count

        elem = self._elem_size(f.type, key, tmpl)
        if elem is not None:
            span = elem * count
            if pos + span > len(data):
                raise NifSpecError("array past end of block")
            if not capture:
                return None, pos + span
            fmt = self._bulk_format(f.type, key, tmpl)
            if fmt is not None and count:
                vals = struct.unpack_from(f"<{fmt * count}", data, pos)
                width = len(fmt)
                if width == 1:
                    out = list(vals)
                else:
                    out = [tuple(vals[i:i + width])
                           for i in range(0, len(vals), width)]
                return out, pos + span
            return None, pos + span

        out = [] if capture else None
        for _ in range(count):
            val, pos = self._read_value(
                data, pos, f.type, key, capture, arg, tmpl, btype)
            if capture:
                out.append(val)                     # type: ignore[union-attr]
        return out, pos

    # -- public API ----------------------------------------------------

    def read_block(self, data: bytes, pos: int, type_name: str, key: tuple,
                   capture: bool = False) -> tuple[dict, int]:
        """Read one block of *type_name* at *pos*; return (values, end_pos)."""
        fields = self._chain(type_name)
        if not fields and type_name not in self._objects:
            raise NifSpecError(f"unknown block type {type_name!r}")
        return self._read_fields(data, pos, fields, key, capture, 0,
                                 None, type_name)

    def walk(self, data: bytes, header, want: "set[str] | None" = None):
        """Yield ``(index, offset, size, values)`` for every block in *data*.

        *values* is the decoded field dict when the block's type is in *want*
        (arrays included) and a scalars-only dict otherwise. Raises
        :class:`NifSpecError` at the first block that cannot be walked, since
        every later offset depends on it.
        """
        key = (header.version, header.user_version, header.bs_version)
        pos = header.body_offset
        want = want or set()
        for i in range(header.num_blocks):
            bt = header.type_of(i)
            if not bt:
                raise NifSpecError(f"block {i} has no type name")
            start = pos
            values, pos = self.read_block(data, pos, bt, key, capture=bt in want)
            yield i, start, pos - start, values


_SPEC: NifSpec | None = None
_SPEC_ERROR: Exception | None = None


def spec_path() -> Path:
    """Location of the vendored nif.xml."""
    return _XML_PATH


def load_spec() -> NifSpec:
    """Parsed nif.xml, loaded once per process (~0.3s, ~400KB of XML)."""
    global _SPEC, _SPEC_ERROR
    if _SPEC is not None:
        return _SPEC
    if _SPEC_ERROR is not None:
        raise _SPEC_ERROR
    try:
        _SPEC = NifSpec()
    except Exception as exc:                        # noqa: BLE001
        _SPEC_ERROR = exc
        raise
    return _SPEC
