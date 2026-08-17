"""
Qt wizard-tool registry + shared Qt wizard views.

Games keep declaring their tools as ``WizardTool`` descriptors (see
``Games.base_game``) whose ``dialog_class_path`` points at the Tk wizard
class.  The Qt app never imports those Tk classes - instead this registry maps
each ``dialog_class_path`` to a Qt view factory.  Tools without an entry here
appear greyed out in the Wizards header menu until their port lands; tools in
``EXCLUDED`` are dropped from the Qt app entirely.

Layout convention (mirrors the Tk side, where shared wizards live in
``wizards/`` and game-specific ones inside their game's folder):
  * multi-game Qt views live in THIS package (``wizards_qt/``);
  * game-specific Qt views live next to their Tk counterpart in the game's
    folder, as ``Games/<Game>/<name>_wizard_qt.py``.
Both kinds are registered here.  Factories lazy-import their view module, so
the Tk app never pulls in Qt code and game-folder views only load on open.

Views open as panel-scoped tabs: ``panel`` picks which panel the tab takes
over ("plugins" for most tools, "modlist" for the wide ones that were
full-width overlays in Tk).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget


@dataclass(frozen=True)
class QtWizardSpec:
    """One ported wizard: how to build its view and which panel it scopes to."""
    # (game, log_fn=..., on_close=..., ctx=QtWizardContext, **extra) -> QWidget
    view_factory: Callable[..., "QWidget"]
    # "plugins" | "modlist" take over one panel; "full" opens a full-UI tab.
    panel: str = "plugins"


@dataclass(frozen=True)
class QtWizardContext:
    """App hooks handed to every wizard view (factories that don't need them
    just ignore the ctx kwarg).

    run_deploy(on_done) starts a deploy through the app's deploy machinery
    (mutex/coalesce + progress popup); on_done(ok: bool) fires on the UI
    thread when the final deploy completes. Returns False if a deploy could
    not be started. run_restore(on_done) is its counterpart for restoring the
    modlist (same machinery; False when a restore can't start) - wizards that
    write files into the game root (4GB patch backup exe, FO3 downgrade)
    restore first, because files created while a profile is deployed are
    missing from the deploy snapshot and the next restore would sweep them
    into overwrite/ as runtime files.
    refresh_modlist() re-syncs the mods folder + reloads the
    panels (footer Refresh).  refresh_plugins() re-runs LOOT to refresh plugin
    metadata WITHOUT reordering the load order (footer Refresh Plugins) - used by
    the xEdit wizards after a clean/edit session so dirty/message flags update.
    import_manifest(manifest, source_stem, bundle_zip) opens the Profile-import
    tab (the collection detail + install pipeline) for an already-parsed
    Amethyst manifest - used by the curated-profile wizard. current_profile()
    returns the LIVE active profile name (profile_name is frozen at open, so a
    wizard spanning a profile switch - e.g. a profile import - reads this).
    nexus_api() returns the app's shared NexusAPI (or None when not logged
    in) - call it on the GUI thread; used by the MPI wizards' hands-free
    archive fetch (premium direct download / download-folder watch).
    open_log_tab() opens (or focuses) the app's Log tab, where every wizard's
    log_fn output already lands - the texture tools call it when they start a
    run so their output is visible without hunting for the docked log.
    set_tool_lock(key, label, held) holds/releases the deploy-restore lock for
    a long-running external tool that reads the DEPLOYED Data folder: while
    held, Deploy/Restore/Play are disabled and refuse to start. Keyed so
    concurrent tools nest; ALWAYS release on the GUI thread in a finally.
    """
    profile_name: str = "default"
    run_deploy: Callable | None = None
    run_restore: Callable | None = None
    refresh_modlist: Callable | None = None
    refresh_plugins: Callable | None = None
    import_manifest: Callable | None = None
    current_profile: Callable | None = None
    nexus_api: Callable | None = None
    open_log_tab: Callable | None = None
    set_tool_lock: Callable | None = None


# Deliberately dropped from the Qt app (not even shown greyed out).
EXCLUDED: frozenset[str] = frozenset({
    "wizards.bepinex.BepInExWizard",
})


def _re_pak_restore(game, log_fn=None, on_close=None, ctx=None, **extra):
    # Lazy import keeps app startup light (matches app.py local-import style).
    from wizards_qt.re_pak_restore_view import RePakRestoreView
    return RePakRestoreView(game, log_fn=log_fn, on_close=on_close, **extra)


def _pandora(game, log_fn=None, on_close=None, ctx=None, **extra):
    from wizards_qt.pandora_view import PandoraView
    return PandoraView(game, log_fn=log_fn, on_close=on_close, ctx=ctx)


def _reshade(game, log_fn=None, on_close=None, ctx=None, **extra):
    # extra carries reshade_dll / reshade_arch from the WizardTool registration.
    from wizards_qt.reshade_view import ReShadeView
    return ReShadeView(game, log_fn=log_fn, on_close=on_close, ctx=ctx, **extra)


def _script_extender(game, log_fn=None, on_close=None, ctx=None, **extra):
    # extra carries github_api_url / download_url / direct_download_url /
    # archive_keywords / versions from each game's WizardTool registration.
    from wizards_qt.script_extender_view import ScriptExtenderView
    return ScriptExtenderView(game, log_fn=log_fn, on_close=on_close,
                              ctx=ctx, **extra)


def _xedit(game, log_fn=None, on_close=None, ctx=None, **extra):
    # extra carries xedit_exe / nexus_url / app_dir / display_name from each
    # game's WizardTool registration (SSEEdit defaults when omitted).
    from wizards_qt.xedit_view import XEditView
    return XEditView(game, log_fn=log_fn, on_close=on_close, ctx=ctx, **extra)


def _xedit_qac(game, log_fn=None, on_close=None, ctx=None, **extra):
    from wizards_qt.xedit_view import XEditView
    return XEditView(game, log_fn=log_fn, on_close=on_close, ctx=ctx,
                     qac=True, **extra)


def _xedit_discord(game, log_fn=None, on_close=None, ctx=None, **extra):
    # extra carries xedit_exe (xFOEdit/xSFEdit/xTESEdit) / app_dir / discord=True.
    from wizards_qt.xedit_view import XEditView
    return XEditView(game, log_fn=log_fn, on_close=on_close, ctx=ctx, **extra)


def _xedit_discord_qac(game, log_fn=None, on_close=None, ctx=None, **extra):
    from wizards_qt.xedit_view import XEditView
    return XEditView(game, log_fn=log_fn, on_close=on_close, ctx=ctx,
                     qac=True, **extra)


def _dyndolod_tool(tool: str):
    def factory(game, log_fn=None, on_close=None, ctx=None, **extra):
        from wizards_qt.dyndolod_view import DynDOLODView
        return DynDOLODView(game, log_fn=log_fn, on_close=on_close, ctx=ctx,
                            tool=tool, **extra)
    return factory


# ---- phase 6 factories ------------------------------------------------------

def _simple(module: str, cls: str):
    """Factory for a single-class view taking (game, log_fn, on_close, ctx)."""
    def factory(game, log_fn=None, on_close=None, ctx=None, **extra):
        import importlib
        view_cls = getattr(importlib.import_module(module), cls)
        return view_cls(game, log_fn=log_fn, on_close=on_close, ctx=ctx, **extra)
    return factory


def _param(module: str, cls: str, **fixed):
    """Factory that injects fixed kwargs (e.g. tool=…) into the view."""
    def factory(game, log_fn=None, on_close=None, ctx=None, **extra):
        import importlib
        view_cls = getattr(importlib.import_module(module), cls)
        return view_cls(game, log_fn=log_fn, on_close=on_close, ctx=ctx,
                        **fixed, **extra)
    return factory


# dialog_class_path → QtWizardSpec.  Keyed by the Tk class path (not tool.id,
# which is per-game suffixed like "run_skygen_skyrimse") so one entry serves
# every game that registers the tool.
REGISTRY: dict[str, QtWizardSpec] = {
    "wizards.re_pak_restore.RePakRestoreWizard": QtWizardSpec(_re_pak_restore),
    "wizards.pandora.PandoraWizard": QtWizardSpec(_pandora),
    "wizards.reshade.ReShadeWizard": QtWizardSpec(_reshade),
    "wizards.script_extender.ScriptExtenderWizard": QtWizardSpec(_script_extender),
    "wizards.sseedit.SSEEditWizard": QtWizardSpec(_xedit),
    "wizards.sseedit.SSEEditQACWizard": QtWizardSpec(_xedit_qac),
    "wizards.sseedit.XEditDiscordWizard": QtWizardSpec(_xedit_discord),
    "wizards.sseedit.XEditDiscordQACWizard": QtWizardSpec(_xedit_discord_qac),
    "wizards.dyndolod.TexGenWizard": QtWizardSpec(_dyndolod_tool("texgen")),
    "wizards.dyndolod.DynDOLODWizard": QtWizardSpec(_dyndolod_tool("dyndolod")),
    "wizards.dyndolod.xLODGenWizard": QtWizardSpec(_dyndolod_tool("xlodgen")),

    # -- phase 6: plugins-panel tools --
    "wizards.mewgenics_gpak.MewgenicsGpakWizard":
        QtWizardSpec(_simple("wizards_qt.gpak_view", "GpakView")),
    "wizards.modio_settings.ModioSettingsWizard":
        QtWizardSpec(_simple("wizards_qt.modio_settings_view", "ModioSettingsView")),
    "wizards.fnv_4gb_patch.Fnv4GbPatchWizard":
        QtWizardSpec(_simple("wizards_qt.fnv_4gb_view", "Fnv4GbView")),
    "wizards.fallout_downgrade.FalloutDowngradeWizard":
        QtWizardSpec(_simple("wizards_qt.fallout_downgrade_view", "FalloutDowngradeView")),
    "wizards.wrye_bash.WryeBashWizard":
        QtWizardSpec(_simple("wizards_qt.wrye_bash_view", "WryeBashView")),
    "wizards.bethini.BethINIWizard":
        QtWizardSpec(_simple("wizards_qt.bethini_view", "BethiniView")),
    "wizards.creationkit.CreationKitWizard":
        QtWizardSpec(_simple("wizards_qt.creationkit_view", "CreationKitView")),
    "wizards.eslifier.ESLifierWizard":
        QtWizardSpec(_simple("wizards_qt.eslifier_view", "ESLifierView")),
    "wizards.pgpatcher.PGPatcherWizard":
        QtWizardSpec(_simple("wizards_qt.pgpatcher_view", "PGPatcherView")),
    "wizards.bodyslide.BodySlideWizard":
        QtWizardSpec(_param("wizards_qt.bodyslide_view", "BodySlideView", tool="bodyslide")),
    "wizards.bodyslide.OutfitStudioWizard":
        QtWizardSpec(_param("wizards_qt.bodyslide_view", "BodySlideView", tool="outfitstudio")),
    # Native-Linux AppImage fork - Qt-only, no Tk counterpart ever existed.
    "wizards.bodyslide_linux.BodySlideLinuxWizard":
        QtWizardSpec(_param("wizards_qt.bodyslide_linux_view",
                            "BodySlideLinuxView", tool="bodyslide")),
    "wizards.bodyslide_linux.OutfitStudioLinuxWizard":
        QtWizardSpec(_param("wizards_qt.bodyslide_linux_view",
                            "BodySlideLinuxView", tool="outfitstudio")),
    "wizards.script_merger_tw3.ScriptMergerWizard":
        QtWizardSpec(_simple("wizards_qt.script_merger_view", "ScriptMergerView")),
    "wizards.vramr.VRAMrWizard":
        QtWizardSpec(_param("wizards_qt.texture_tool_view", "TextureToolView", tool="vramr")),
    "wizards.bendr_parallaxr.BENDrWizard":
        QtWizardSpec(_param("wizards_qt.texture_tool_view", "TextureToolView", tool="bendr")),
    "wizards.bendr_parallaxr.ParallaxRWizard":
        QtWizardSpec(_param("wizards_qt.texture_tool_view", "TextureToolView", tool="parallaxr")),
    "wizards.ttw.TTWInstallerWizard":
        QtWizardSpec(_simple("wizards_qt.ttw_view", "TTWView")),
    "wizards.bsa_decompressor.BSADecompressorWizard":
        QtWizardSpec(_simple("wizards_qt.bsa_decompressor_view",
                             "BSADecompressorView")),
    "wizards.esm_fixes.ESMFixesWizard":
        QtWizardSpec(_simple("wizards_qt.esm_fixes_view", "ESMFixesView")),
    "wizards.curated_profile.CuratedProfileWizard":
        QtWizardSpec(_simple("wizards_qt.curated_profile_view",
                             "CuratedProfileView")),
    "wizards.dtkit_patch.DtkitPatchWizard":
        QtWizardSpec(_simple("wizards_qt.dtkit_patch_view", "DtkitPatchView")),
    "Games.Morrowind.mgexe_wizard.MGEXEWizard":
        QtWizardSpec(_simple("Games.Morrowind.mgexe_wizard_qt", "MGEXEView")),
    "Games.Morrowind.mcp_wizard.MCPWizard":
        QtWizardSpec(_simple("Games.Morrowind.mcp_wizard_qt", "MCPView")),

    # -- phase 6: modlist-panel tools (Tk _full_width_overlay) --
    "wizards.sse_display_tweaks.SSEDisplayTweaksWizard":
        QtWizardSpec(_simple("wizards_qt.sdt_view", "SDTView"), panel="modlist"),
    "wizards.engine_fixes.EngineFixesWizard":
        QtWizardSpec(_simple("wizards_qt.engine_fixes_view", "EngineFixesView"), panel="modlist"),
    "wizards.skygen.SkyGenWizard":
        QtWizardSpec(_simple("wizards_qt.skygen_view", "SkyGenView"), panel="modlist"),
    "wizards.plugin_audit.PluginAuditWizard":
        QtWizardSpec(_simple("wizards_qt.plugin_audit_view", "PluginAuditView"), panel="modlist"),

    # -- full-UI tools (tree + 3D viewport needs the whole window) --
    "wizards.nif_viewer.NifViewerWizard":
        QtWizardSpec(_simple("wizards_qt.nif_viewer_view", "NifViewerView"),
                     panel="full"),
    "wizards.npc_viewer.NpcViewerWizard":
        QtWizardSpec(_simple("wizards_qt.npc_viewer_view", "NpcViewerView"),
                     panel="full"),

    # -- ported former external plugins (attached per game_id via
    #    Utils.plugin_loader.BUILTIN_WIZARD_TOOLS) --
    "wizards.bethesda_register_game_path.RegisterGamePathWizard":
        QtWizardSpec(_simple("wizards_qt.bethesda_register_view", "RegisterGamePathView")),
    "wizards.bethesda_synthesis.SynthesisWizard":
        QtWizardSpec(_simple("wizards_qt.synthesis_view", "SynthesisView")),
    "wizards.bg3_import.BG3ImportWizard":
        QtWizardSpec(_simple("wizards_qt.bg3_import_view", "BG3ImportView")),
    "wizards.sdv_smapi.SmapiWizard":
        QtWizardSpec(_simple("wizards_qt.smapi_view", "SmapiView")),
    "wizards.sr_srml.SRMLWizard":
        QtWizardSpec(_simple("wizards_qt.srml_view", "SRMLView")),
    "wizards.msc_mscloader.MSCLoaderWizard":
        QtWizardSpec(_simple("wizards_qt.mscloader_view", "MSCLoaderView")),
    # Qt-only tool: no Tk class exists behind this path, it is a registry key.
    "wizards.special_k.SpecialKWizard":
        QtWizardSpec(_simple("wizards_qt.special_k_view", "SpecialKView")),
}


def get_spec(dialog_class_path: str) -> QtWizardSpec | None:
    return REGISTRY.get(dialog_class_path)
