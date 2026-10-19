from gui_qt.dialogs.repositories import RepositoriesOverlay


class PluginRepositoriesOverlay(RepositoriesOverlay):
    def __init__(self, host, store, on_done=None):
        from amethyst_plugins.sources import normalize_source
        super().__init__(
            host, store, on_done=on_done, normalize_source=normalize_source,
            title=self.tr("Plugin repositories"),
            hint=self.tr("Choose a folder containing plugin subfolders, or a single plugin's folder. "
                         "Blank branch uses the default branch; blank folder uses the repository root."),
            removed_message=self.tr("Repository removed. Installed plugins are kept."))
