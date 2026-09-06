from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class Archive:
    key: str
    name: str
    size: int
    kind: str
    state: dict


@dataclass(frozen=True)
class Directive:
    index: int
    kind: str
    path: str
    hash: str
    size: int
    data: dict
    embedded_hash: str = ""
    embedded_size: int | None = None

    @property
    def output_hash(self):
        return self.embedded_hash or self.hash

    @property
    def output_size(self):
        return self.embedded_size if self.embedded_size is not None else self.size

    @property
    def deterministic(self) -> bool:
        return self.kind not in {"RemappedInlineFile", "CreateBSA", "TransformedTexture", "ArchiveMeta"}


@dataclass
class Package:
    path: Path
    identity: str
    name: str
    version: str
    game: str
    archives: dict[str, Archive]
    directives: list[Directive]
    metadata: dict
    profiles: list[str]
    selected_profile: str = ""
    game_path: str = ""


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    detail: str


@dataclass
class PreflightReport:
    checks: list[Check] = field(default_factory=list)
    cached: dict[str, Path] = field(default_factory=dict)
    game_files: dict[str, Path] = field(default_factory=dict)
    download_bytes: int = 0
    install_bytes: int = 0

    @property
    def ok(self):
        return not any(c.status == "error" for c in self.checks)


@dataclass
class InstallRequest:
    package: Package
    game: object
    directory: Path
    downloads: Path
    profiles: list[str]
    game_roots: dict[str, Path]
    api: object = None
    premium: bool = False
    mode: str = "install"
    gallery_id: str = ""
    gallery_metadata: dict = field(default_factory=dict)
    fixes: list[str] = field(default_factory=list)
    texconv: Path | None = None
    proton: Path | None = None
    resolve_conflicts: Callable | None = None

    def __post_init__(self):
        self.directory = Path(self.directory).expanduser().absolute()
        self.downloads = Path(self.downloads).expanduser().absolute()
        self.game_roots = {k: Path(v).expanduser().absolute() for k, v in self.game_roots.items()}


@dataclass(frozen=True)
class Conflict:
    path: str
    reason: str
    old_hash: str | None
    current_hash: str | None
    new_hash: str | None
    current_path: str = ""
    author_path: str = ""


@dataclass
class InstallResult:
    status: str
    profiles: list[Path] = field(default_factory=list)
    selected_profile: str = ""
    installed: int = 0
    message: str = ""
