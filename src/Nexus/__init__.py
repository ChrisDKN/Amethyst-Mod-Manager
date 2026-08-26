"""
Nexus Mods integration package.

Provides API access, NXM protocol handling, and download management
for the Nexus Mods ecosystem.
"""

from importlib import import_module

__all__ = ["NexusAPI", "NexusModUpdateInfo", "NxmHandler", "NxmLink", "NxmIPC", "NexusDownloader",
           "NexusModMeta", "read_meta", "write_meta", "build_meta_from_download",
           "scan_installed_mods", "check_for_updates", "UpdateInfo",
           "check_missing_requirements", "check_requirements_from_gql", "MissingRequirementInfo"]

# Importing a submodule first executes this package file. Eagerly importing
# every integration here meant the lightweight startup-only
# ``Nexus.nxm_handler`` import also pulled in requests, keyring, cryptography,
# metadata, and update-checking code. Preserve the package's public re-exports
# while loading each one only when a caller actually requests it (PEP 562).
_EXPORTS = {
    "NexusAPI": ("nexus_api", "NexusAPI"),
    "NexusModUpdateInfo": ("nexus_api", "NexusModUpdateInfo"),
    "NxmHandler": ("nxm_handler", "NxmHandler"),
    "NxmLink": ("nxm_handler", "NxmLink"),
    "NxmIPC": ("nxm_handler", "NxmIPC"),
    "NexusDownloader": ("nexus_download", "NexusDownloader"),
    "NexusModMeta": ("nexus_meta", "NexusModMeta"),
    "read_meta": ("nexus_meta", "read_meta"),
    "write_meta": ("nexus_meta", "write_meta"),
    "build_meta_from_download": ("nexus_meta", "build_meta_from_download"),
    "scan_installed_mods": ("nexus_meta", "scan_installed_mods"),
    "check_for_updates": ("nexus_update_checker", "check_for_updates"),
    "UpdateInfo": ("nexus_update_checker", "UpdateInfo"),
    "check_missing_requirements": (
        "nexus_requirements", "check_missing_requirements"),
    "check_requirements_from_gql": (
        "nexus_requirements", "check_requirements_from_gql"),
    "MissingRequirementInfo": (
        "nexus_requirements", "MissingRequirementInfo"),
}


def __getattr__(name: str):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(f".{module_name}", __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
