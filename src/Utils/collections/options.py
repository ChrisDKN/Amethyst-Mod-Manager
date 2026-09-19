from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

BUNDLE_PENDING = "Local bundle restoration pending"


@dataclass(frozen=True)
class CollectionInstallOptions:
    mode: Literal["new", "append", "group", "continue"] = "new"
    target: str = ""
    group_name: str = ""
    reuse_profile: str = ""
    overwrite_existing: bool = False
    skip_existing: bool = False
    target_is_group: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value) -> CollectionInstallOptions:
        value = value if isinstance(value, dict) else {}
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})


@dataclass
class CollectionInstallReport:
    missing_required: list[str] = field(default_factory=list)
    failed_stages: list[str] = field(default_factory=list)
    intentional_skips: list[str] = field(default_factory=list)
    required_folders: list[str] = field(default_factory=list)
    verified: bool = False

    @property
    def ready(self) -> bool:
        return self.verified and not self.missing_required and not self.failed_stages

    def to_dict(self) -> dict:
        return asdict(self)
