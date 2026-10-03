import codecs
from encodings.cp1252 import decoding_table


# Windows preserves the five control bytes left undefined by Python's CP1252.
_DECODING_TABLE = "".join(chr(i) if char == "\ufffe" else char
                          for i, char in enumerate(decoding_table))
_ENCODING_TABLE = codecs.charmap_build(_DECODING_TABLE)


def encode_bsa_name(name: str, errors: str = "strict") -> bytes:
    return codecs.charmap_encode(name, errors, _ENCODING_TABLE)[0]


def decode_bsa_name(data: bytes) -> str:
    return codecs.charmap_decode(data, "strict", _DECODING_TABLE)[0]
