from pathlib import PurePosixPath
import zipfile


def is_kcd_pak(path) -> bool:
    try:
        with open(path, "rb") as stream:
            return stream.read(4) in (b"PK\x03\x04", b"PK\x05\x06")
    except OSError:
        return False


def read_pak_file_list(path) -> list[str]:
    try:
        with zipfile.ZipFile(path) as archive:
            return [item.filename.replace("\\", "/").lower()
                    for item in archive.infolist() if not item.is_dir()]
    except (OSError, ValueError, zipfile.BadZipFile):
        return []


def member_path(archive: str, member: str) -> str:
    parts = PurePosixPath(archive.replace("\\", "/").lower()).parts
    for index in range(len(parts) - 2, -1, -1):
        part = parts[index]
        if part == "data":
            return "/".join((*parts[index:-1], member))
        if part == "localization":
            return "/".join((*parts[index:-1], PurePosixPath(parts[-1]).stem, member))
    return member
