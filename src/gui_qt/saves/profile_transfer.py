from pathlib import Path
import zipfile

from PySide6.QtCore import QObject, Signal

from gui_qt.dialogs.confirm import ConfirmOverlay
from gui_qt.dialogs.list_picker import ListPickerOverlay
from gui_qt.runtime.worker import run_in_worker
from Utils.saves.paths import save_transfer_locations
from Utils.wine.manager import fmt_size


class ProfileSavesTransfer(QObject):
    _resolved = Signal(object, str)
    _imported = Signal(bool, str)

    def __init__(self, host, game, profile_name, notify, log_fn, *,
                 on_export=None, bundle=None, on_import=None):
        super().__init__(host)
        self._host = host
        self._game = game
        self._profile_name = profile_name
        self._notify = notify
        self._log = log_fn
        self._on_export = on_export
        self._bundle = bundle
        self._on_import = on_import
        self._resolved.connect(self._on_resolved)
        self._imported.connect(self._on_imported)
        run_in_worker(self._resolve, self._resolved, unpack=True,
                      name="profile-save-locations",
                      error_result=([], self.tr("Could not resolve save folders.")))

    def _resolve(self):
        if self._bundle is not None:
            with zipfile.ZipFile(self._bundle) as zf:
                if "saves/saves.zip" not in zf.namelist():
                    return None, ""
        locations = save_transfer_locations(
            self._game, self._profile_name, include_missing_profile=self._bundle is not None)
        if self._bundle is None:
            locations = [loc for loc in locations if loc[0].is_dir()]
        return locations, ""

    def _on_resolved(self, locations, error):
        if locations is None:
            self.deleteLater()
            return
        if error or not locations:
            self._notify(error or self.tr("No save folders found for this game."), "warning")
            self.deleteLater()
            return
        if len(locations) == 1:
            self._picked(locations[0])
            return
        items = []
        for loc in locations:
            label = str(loc[0])
            if loc[2]:
                label += self.tr(" [profile saves]")
            if loc[4]:
                label += " (" + ", ".join(loc[4]) + ")"
            items.append((label, loc))
        ListPickerOverlay.show_over(
            self._host, self.tr("Choose save folder to import into") if self._bundle
            else self.tr("Choose save folder to export"), items, self._picked,
            select_label=self.tr("Select"))

    def _picked(self, location):
        if location is None:
            self.deleteLater()
            return
        if self._bundle is None:
            self._on_export(location)
            self.deleteLater()
            return
        path, _prefix, _profile, _exists, patterns = location
        moved = self.tr('The current contents are moved aside to a "{0}" folder first, '
                        'so nothing is lost.').format(path.name + ".before-import-…")
        if patterns:
            moved = self.tr('The current {0} files are moved aside to a "{1}" folder first, '
                            'so nothing is lost.').format(
                ", ".join(patterns), path.name + ".before-import-…")
        ConfirmOverlay.show_over(
            self._host, self.tr("Import bundled saves?"),
            self.tr("This profile includes a saves archive.\n\nExtract it into\n{0}\n\n{1}")
            .format(path, moved),
            lambda ok: self._start_import(path, patterns) if ok else self.deleteLater(),
            confirm_label=self.tr("Import"), cancel_label=self.tr("Skip"), card_h=320)

    def _start_import(self, path, patterns):
        self._notify(self.tr("Extracting saves…"), "info")

        def worker():
            from Utils.saves.transfer import import_bundled_saves
            try:
                count, size, backup = import_bundled_saves(
                    Path(self._bundle), path, patterns=patterns)
                if backup is not None:
                    self._log(f"[saves] previous saves moved to {backup}")
                self._log(f"[saves] imported {count} file(s), {fmt_size(size)} → {path}")
                return True, self.tr("Imported {0} save file(s) ({1}).").format(count, fmt_size(size))
            except Exception as exc:
                self._log(f"[saves] bundled save import failed: {exc}")
                return False, self.tr("Save import failed: {0}").format(exc)

        run_in_worker(worker, self._imported, unpack=True, name="profile-saves-import",
                      error_result=(False, self.tr("Save import failed.")))

    def _on_imported(self, ok, message):
        self._notify(message, "success" if ok else "error")
        if ok and self._on_import is not None:
            self._on_import()
        self.deleteLater()
