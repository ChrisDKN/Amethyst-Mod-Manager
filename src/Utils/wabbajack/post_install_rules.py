"""Wabbajack post-install definitions; execution stays in the existing setup modules.

Add list matches and reuse the rules below. Keep adjustment and task IDs stable:
installed profiles and saved setup choices refer to them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .games import nexus_domain, token
from .manifest import stock_folder


@dataclass(frozen=True)
class Match:
    domain: str = ""
    game_id: str = ""
    names: tuple[str, ...] = ()
    name_contains: tuple[str, ...] = ()
    include_profiles: bool = False

    def matches(self, package, game=None):
        if self.domain and nexus_domain(package.game) != self.domain:
            return False
        if self.game_id and getattr(game, "game_id", "") != self.game_id:
            return False
        if self.names:
            if self.include_profiles:
                names = {token(package.name), *(token(name) for name in package.profiles)}
                if not names.intersection(token(name) for name in self.names):
                    return False
            elif package.name.casefold().strip() not in {name.casefold() for name in self.names}:
                return False
        return not self.name_contains or any(name.casefold() in package.name.casefold() for name in self.name_contains)


@dataclass(frozen=True)
class Adjustment:
    id: str
    label: str
    required: bool = False


@dataclass(frozen=True)
class Notice:
    status: str
    name: str
    detail: str
    unless_game_setting: str = ""


@dataclass(frozen=True)
class StockCopy:
    folder: str
    source_game: str
    executable: str
    folders: tuple[str, ...]
    root_suffixes: tuple[str, ...]
    exclude_names: tuple[str, ...]
    source_error: str


@dataclass(frozen=True)
class OmitStockFiles:
    adjustment: Adjustment
    names: tuple[str, ...]


@dataclass(frozen=True)
class ModlistRule:
    id: str
    match: Match
    runtime_dependencies: tuple[str, ...] = ()
    stock_copy: StockCopy | None = None
    omit_stock_files: tuple[OmitStockFiles, ...] = ()
    notices: tuple[Notice, ...] = ()


MODLIST_RULES = (
    ModlistRule(
        id="nuclear-sunset",
        match=Match(domain="newvegas", names=("Nuclear Sunset",)),
        runtime_dependencies=("d3dcompiler_43",),
        stock_copy=StockCopy(
            folder="[NoDelete] Stock New Vegas",
            source_game="newvegas",
            executable="FalloutNV.exe",
            folders=("data", "fallout new vegas", "redists", "directx"),
            root_suffixes=(".exe", ".dll", ".ini", ".vdf"),
            exclude_names=("falloutnv_backup.exe", "fnvpatch.exe", "patcher.exe"),
            source_error="Select the original New Vegas installation for the stock-game copy",
        ),
        omit_stock_files=(OmitStockFiles(
            Adjustment("nuclear:proton-dxvk", "Use Proton's DXVK for the Nuclear Sunset stock game (omit bundled d3d9.dll and dxvk.conf)"),
            ("d3d9.dll", "dxvk.conf"),
        ),),
        notices=(
            Notice("warning", "Author instructions", "Nuclear Sunset's Linux guide specifies Proton 11 and a separate YUPTTW update. Confirm the selected runtime and external content match the guide before launch."),
            Notice("error", "Stock game patch", "Enable automatic New Vegas 4GB patching so deployment patches the managed stock executable", unless_game_setting="auto_4gb_patch"),
        ),
    ),
    ModlistRule(
        id="fallout4vr-essentials",
        match=Match(game_id="Fallout4VR", name_contains=("essentials",)),
        runtime_dependencies=("vcrun2012",),
    ),
)


@dataclass(frozen=True)
class OutputRule:
    id: str
    label: str
    domain: str
    aliases: tuple[str, ...]
    masters: tuple[str, ...]
    mpi_titles: tuple[str, ...] = ()
    required_plugin: str = ""
    companion_master: str = ""
    companion_alias: str = ""
    prefer_unprefixed: bool = False
    exact_aliases: tuple[str, ...] = ()


OUTPUT_RULES = (
    OutputRule("ttw", "Tale of Two Wastelands", "newvegas",
               ("tale of two wastelands", "ttw output"), ("TaleOfTwoWastelands.esm",),
               ("Tale of Two Wastelands",), required_plugin="taleoftwowastelands.esm",
               companion_master="YUPTTW.esm", companion_alias="yupttw update", exact_aliases=("ttw",)),
    OutputRule("yupttw", "YUPTTW update", "newvegas", ("yupttw update",), ("YUPTTW.esm",)),
    OutputRule("fnv-esm", "Ultimate Edition ESM Fixes Remastered", "newvegas",
               ("ultimate edition esm fixes",), ("FalloutNV.esm",), ("Ultimate Edition ESM Fixes Remastered",)),
    OutputRule("fo3-esm", "Unofficial Fallout 3 ESM Patcher", "fallout3",
               ("unofficial fallout 3 esm patcher",), ("Fallout3.esm",), ("Unofficial Fallout 3 ESM Patcher",)),
    OutputRule("fo3-bsa", "Fallout 3 BSA Decompressor", "fallout3",
               ("fallout 3 bsa decompressor", "fo3 bsa decompressor", "decompressed bsas"),
               ("Fallout - Meshes.bsa", "Fallout - Misc.bsa", "Fallout - Textures.bsa"),
               ("Fallout 3 BSA Decompressor", "Fallout: 3 BSA Decompressor"), prefer_unprefixed=True),
)


@dataclass(frozen=True)
class BSARule:
    domain: str
    names: tuple[str, ...]
    tools: tuple[str, ...]
    reason: str
    bundled_reason: str


BSA_RULES = (
    BSARule(
        domain="newvegas",
        names=("Viva New Vegas", "Viva New Vegas Extended", "Mojave Express"),
        tools=("VanillaBSAsPatcher.exe", "FNVBSADecompressor.exe", "FNVBSADecompressor.mpi"),
        reason="This list requires the New Vegas BSA decompression and audio fixes",
        bundled_reason="The package includes a New Vegas vanilla BSA patcher",
    ),
)


@dataclass(frozen=True)
class BSARequirement:
    reason: str
    folders: tuple[str, ...] = ()


FOLDER_NOTICES = (
    ("radio fix", Notice("manual", "Author instructions", "The supplied Radio Fix requires audio conversion after reconstruction. Its batch-script format has not been verified for automatic processing; follow the author's radio setup instructions before launching.")),
)

DLL_OVERRIDES = ("dinput8", "version", "winhttp", "winmm")


@dataclass(frozen=True)
class DisplayRule:
    scope: str
    names: tuple[str, ...]
    section: str
    values: tuple[tuple[str, str], ...]
    suffix: str = ""


DISPLAY_RULES = (
    DisplayRule("profile", ("oblivion.ini", "falloutcustom.ini", "skyrimcustom.ini", "fallout76custom.ini"),
                "Display", (("iSize W", "{width}"), ("iSize H", "{height}")), suffix="prefs.ini"),
    DisplayRule("profile", ("user.settings", "dx12user.settings"),
                "Viewport", (("Resolution", "{width}x{height}"),)),
    DisplayRule("mod", ("ssedisplaytweaks.ini",), "Render", (("Resolution", "{width}x{height}"),)),
)


def matching_rules(package, game=None):
    return tuple(rule for rule in MODLIST_RULES if rule.match.matches(package, game))


def stock_copy_rule(request):
    stock = stock_folder(request.package)
    return next((rule.stock_copy for rule in matching_rules(request.package, request.game)
                 if rule.stock_copy and rule.stock_copy.folder.casefold() == stock.casefold()), None)


def post_install_notices(request):
    for rule in matching_rules(request.package, request.game):
        for notice in rule.notices:
            if not notice.unless_game_setting or not getattr(request.game, notice.unless_game_setting, False):
                yield notice
    folders = {d.path.split("/")[0].strip("_ ").casefold() for d in request.package.directives}
    for folder, notice in FOLDER_NOTICES:
        if folder in folders:
            yield notice


def stock_file_adjustments(package, game):
    return [step.adjustment for rule in matching_rules(package, game) for step in rule.omit_stock_files]


def omitted_stock_paths(request):
    stock = stock_folder(request.package)
    if not stock:
        return set()
    return {f"root/{stock}/{name}".casefold()
            for rule in matching_rules(request.package, request.game) for step in rule.omit_stock_files
            if step.adjustment.id in request.fixes for name in step.names}


def runtime_dependencies(package, game, paths, active_names):
    dependencies = list(getattr(game, "auto_install_deps", []) or [])
    dependencies.extend(component for rule in matching_rules(package, game) for component in rule.runtime_dependencies)
    if "netscriptframework.runtime.dll" in active_names and any("skse64_1_5_97.dll" in path for path in paths):
        dependencies.append("dotnet48")
    return [(component, " (required by the enabled .NET Script Framework mod)" if component == "dotnet48" else "")
            for component in dict.fromkeys(dependencies)]


def compatibility_adjustments(paths, native):
    result = []
    root_dlls = {p.rsplit("/", 1)[-1] for p in paths if "/root/" in p or p.count("/") <= 1}
    for name in DLL_OVERRIDES:
        if not native and name + ".dll" in root_dlls:
            result.append(Adjustment("dll:" + name, f"Load the provided {name}.dll before Wine's built-in DLL"))
    if any("enbseries" in p or "enblocal.ini" in p for p in paths):
        result.append(Adjustment("enb-warning", "Acknowledge ENB requires manual Linux compatibility review"))
    if any("net script framework" in p or "netscriptframework" in p for p in paths):
        result.append(Adjustment("framework-warning", "Acknowledge .NET Script Framework requires Windows compatibility review for native Linux"
                                 if native else "Acknowledge .NET Script Framework may require additional Wine configuration"))
    return result


def bsa_requirement(package):
    for rule in BSA_RULES:
        if nexus_domain(package.game) != rule.domain:
            continue
        tools = {token(name) for name in rule.tools}
        folders = {d.path.split("/")[0] for d in package.directives
                   if len(d.path.split("/")) == 2 and token(Path(d.path).name) in tools}
        if folders:
            return BSARequirement(rule.bundled_reason, tuple(sorted(folders)))
        if rule.names and Match(names=rule.names, include_profiles=True).matches(package):
            return BSARequirement(rule.reason)
    return None


def display_rule(path, *, installed=False):
    profile = path.startswith("profiles/") and ("/ini files/" in path if installed else len(path.split("/")) == 3)
    mod = path.startswith("root/mods/" if installed else "mods/")
    if not profile and not mod:
        return None
    name = path.rsplit("/", 1)[-1].casefold()
    for rule in DISPLAY_RULES:
        if (rule.scope == "profile" and profile) or (rule.scope == "mod" and mod):
            if name in rule.names or (rule.suffix and name.endswith(rule.suffix)):
                return rule
    return None


def display_signature(display, signature):
    return f"display:1:{display[0]}x{display[1]}:" + signature
