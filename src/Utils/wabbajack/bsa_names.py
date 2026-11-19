from Utils.bsa.encoding import encode_bsa_name


def encode_name(name, version):
    return name.encode("utf-8") if version == 104 else encode_bsa_name(name)


def _polynomial(data):
    value = 0
    for byte in data:
        value = (value * 0x1003f + byte) & 0xffffffff
    return value


def hash_name(name, version, *, folder=False):
    if version != 104:
        from Utils.bsa.writer import tes4_hash_file, tes4_hash_folder
        return tes4_hash_folder(name) if folder else tes4_hash_file(name)
    name = name.replace("/", "\\").lower()
    root, separator, extension = name.rpartition(".") if not folder else (name, "", "")
    if not separator:
        root, extension = name, ""
    else:
        extension = "." + extension
    if not root:
        return 0
    # Wabbajack hashes the low byte of each UTF-16 character, not the encoded name.
    data = root.encode("utf-16-le")[::2]
    low = (data[-1] | (data[-2] << 8 if len(data) >= 3 else 0)
           | ((len(data) & 0xff) << 16) | (data[0] << 24))
    low |= {".kf": 0x80, ".nif": 0x8000, ".dds": 0x8080, ".wav": 0x80000000}.get(extension, 0)
    high = (_polynomial(data[1:-2])
            + _polynomial(extension.encode("utf-16-le")[::2])) & 0xffffffff
    return (high << 32) | low
