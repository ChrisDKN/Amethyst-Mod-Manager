"""Reusable launch-time virtual filesystem support.

Game handlers opt in and supply their normal game/mod-data paths and deploy
metadata. The default backend binds a materialized profile-local shadow tree;
legacy native/FUSE overlay manifests remain supported for compatibility.
"""

from .overlay import (
    BACKEND_FUSE,
    BACKEND_KERNEL,
    BACKEND_SHADOW,
    MANIFEST_NAME,
    MANIFEST_VERSION,
    RUNTIME_NAME,
    STATE_DIR_NAME,
    bubblewrap_status,
    build_layers,
    cleanup_deployment,
    finalize_deployment,
    fuse_overlay_status,
    manifest_path,
    prefer_virtual_executable,
    state_dir,
    virtual_data_write_path,
    virtual_file,
    virtual_file_path,
    virtual_root_write_path,
    wrap_command,
)
from .game import ProfileVFSGameMixin

__all__ = (
    "BACKEND_FUSE",
    "BACKEND_KERNEL",
    "BACKEND_SHADOW",
    "MANIFEST_NAME",
    "MANIFEST_VERSION",
    "ProfileVFSGameMixin",
    "RUNTIME_NAME",
    "STATE_DIR_NAME",
    "bubblewrap_status",
    "build_layers",
    "cleanup_deployment",
    "finalize_deployment",
    "fuse_overlay_status",
    "manifest_path",
    "prefer_virtual_executable",
    "state_dir",
    "virtual_data_write_path",
    "virtual_file",
    "virtual_file_path",
    "virtual_root_write_path",
    "wrap_command",
)
