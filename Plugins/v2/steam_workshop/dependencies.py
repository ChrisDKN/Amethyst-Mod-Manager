from types import SimpleNamespace

from gui_qt.thunderstore.dependencies import ThunderstoreDepsOverlay
from Utils.downloads.core import fmt_size


class WorkshopDepsOverlay(ThunderstoreDepsOverlay):
    def __init__(self, host, *, root, dependencies, notice='', installed=None, on_done=None):
        self._installed = dict(installed or {})
        super().__init__(host, root=root, dependencies=dependencies, on_done=on_done)
        body = self._card.layout().itemAt(1).widget()
        text = body.text()
        if self._installed:
            text = self.tr('{0} needs {1} other mod(s). Selected requirements already installed '
                           'will be enabled in this profile; the others will be downloaded and installed first.').format(
                               root.full_name, len(self._deps))
        if notice:
            text += '\n' + notice
        body.setText(text)

    def _dep_row(self, palette, package, note):
        name = self._installed.get(package.package_id)
        if name:
            note = self.tr('enable installed mod: {0}').format(name)
            package = SimpleNamespace(package_id=package.package_id, full_name=package.full_name, file_size=0)
        return super()._dep_row(palette, package, note)

    def _update_total(self, *_):
        chosen = self._selected()
        total = self._root.file_size + sum(package.file_size for package in chosen
                                          if package.package_id not in self._installed)
        self._total_lbl.setText(
            self.tr('{0} of {1} dependencies selected - {2} to download').format(
                len(chosen), len(self._deps), fmt_size(total)))

    @classmethod
    def show_over(cls, host, *, root, dependencies, notice='', installed=None, on_done=None):
        top = host.window() if host is not None else None
        return cls(top or host, root=root, dependencies=dependencies, notice=notice,
                   installed=installed, on_done=on_done)
