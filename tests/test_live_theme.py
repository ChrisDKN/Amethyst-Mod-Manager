"""Headless regression tests for live Qt theme switching and editing."""

from __future__ import annotations

import gc
import os
from pathlib import Path
import sys
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch
import weakref


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt  # noqa: E402
from PySide6.QtGui import QColor, QPalette  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QPushButton, QStackedWidget, QTreeView, QWidget,
)

from Utils.themes import load_palettes as real_load_palettes  # noqa: E402
from gui_qt import theme_editor_groups, theme_qt  # noqa: E402
from gui_qt.data_model import DataModel, _DataNode  # noqa: E402
from gui_qt.detachable_tabs import DetachableTabWidget  # noqa: E402
from gui_qt.mod_files_delegate import ModFilesDelegate  # noqa: E402
from gui_qt.mod_files_model import ModFilesModel, _Node  # noqa: E402
from gui_qt.notifications import NotificationManager, ProgressPopup  # noqa: E402
from gui_qt.settings_view import SettingsView  # noqa: E402
from gui_qt.theme_editor_view import ThemeEditorView  # noqa: E402
from gui_qt.theme_preview import ThemePreviewPanel  # noqa: E402


class LiveThemeRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        palettes = real_load_palettes()
        self.palettes = {
            "dark": dict(palettes["dark"]),
            "light": dict(palettes["light"]),
        }
        # The built-in themes intentionally share some specialised model
        # colours. Make them contrast here so this test verifies the model and
        # delegate notification path instead of relying on a redundant repaint.
        self.palettes["light"].update({
            "FILE_DIM": "#135790",
            "FILE_LOSE": "#246801",
            "FILE_WIN": "#abcdef",
            "CONFLICT_HL_ANCHOR": "#fedcba",
        })
        self.mode = "dark"
        self.load_patch = patch.object(
            theme_qt, "load_palettes", side_effect=lambda: self.palettes)
        self.mode_patch = patch.object(
            theme_qt, "get_appearance_mode", side_effect=lambda: self.mode)
        self.load_patch.start()
        self.mode_patch.start()
        theme_qt.invalidate_palette_cache()
        theme_qt.apply_theme(self.app)
        self.widgets: list[QWidget] = []

    def tearDown(self):
        for widget in self.widgets:
            widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()
        self.mode_patch.stop()
        self.load_patch.stop()
        theme_qt.invalidate_palette_cache()

    def test_persisted_switch_updates_widgets_palette_delegate_and_models(self):
        label = QLabel("existing")
        label.setStyleSheet(
            f"color:{theme_qt._c(theme_qt.active_palette(), 'TEXT_MAIN')};")
        view = QTreeView()
        delegate = ModFilesDelegate(view)
        mod_model = ModFilesModel()
        data_model = DataModel()
        mod_root = _Node("", "", is_dir=True)
        mod_child = _Node("file.txt", "file.txt", is_dir=False,
                          parent=mod_root)
        mod_root.children.append(mod_child)
        mod_model.set_root(mod_root, {"file.txt": mod_child})
        data_root = _DataNode("", "", is_dir=True)
        data_child = _DataNode("file.txt", "file.txt", is_dir=False,
                               parent=data_root, mod="Example")
        data_root.children.append(data_child)
        data_model.set_root(data_root)
        mod_changed: list[bool] = []
        data_changed: list[bool] = []
        mod_model.dataChanged.connect(lambda *_: mod_changed.append(True))
        data_model.dataChanged.connect(lambda *_: data_changed.append(True))
        self.widgets.extend((label, view))

        old_delegate = delegate.c_text.name()
        self.mode = "light"
        applied = theme_qt.apply_theme(self.app)

        self.assertEqual(applied["TEXT_MAIN"], self.palettes["light"]["TEXT_MAIN"])
        self.assertIn(self.palettes["light"]["TEXT_MAIN"], label.styleSheet())
        self.assertEqual(
            self.app.palette().color(QPalette.WindowText).name(),
            self.palettes["light"]["TEXT_MAIN"].lower())
        self.assertNotEqual(delegate.c_text.name(), old_delegate)
        self.assertEqual(
            delegate.c_text.name(), self.palettes["light"]["TEXT_MAIN"].lower())
        self.assertEqual(
            mod_model._c_dim.name(), self.palettes["light"]["FILE_DIM"].lower())
        self.assertEqual(
            data_model._c_highlight.name(),
            self.palettes["light"]["CONFLICT_HL_ANCHOR"].lower())
        self.assertTrue(mod_changed)
        self.assertTrue(data_changed)

        # A consumer opened after the switch is initialized from the runtime
        # palette immediately, rather than waiting for the next change.
        late_view = QTreeView()
        late_delegate = ModFilesDelegate(late_view)
        late_label = QLabel("late")
        self.widgets.extend((late_view, late_label))
        self.assertEqual(
            late_delegate.c_text.name(),
            self.palettes["light"]["TEXT_MAIN"].lower())
        self.assertEqual(
            late_label.palette().color(QPalette.WindowText).name(),
            self.palettes["light"]["TEXT_MAIN"].lower())

    def test_preview_is_copied_and_does_not_change_persisted_selection(self):
        preview = dict(self.palettes["light"])
        applied = theme_qt.apply_theme(self.app, preview)
        late_view = QTreeView()
        late_delegate = ModFilesDelegate(late_view)
        self.widgets.append(late_view)
        preview["TEXT_MAIN"] = "#123456"

        self.assertEqual(self.mode, "dark")
        self.assertIsNot(applied, preview)
        self.assertNotEqual(theme_qt.active_palette()["TEXT_MAIN"], "#123456")
        self.assertEqual(
            late_delegate.c_text.name(),
            self.palettes["light"]["TEXT_MAIN"].lower())

        restored = theme_qt.apply_theme(self.app)
        self.assertEqual(restored["TEXT_MAIN"], self.palettes["dark"]["TEXT_MAIN"])

    def test_short_hex_tokens_are_regenerated_too(self):
        sheet = f"color:{theme_qt._c({'TEXT_MAIN': '#fff'}, 'TEXT_MAIN')};"
        rendered = theme_qt._render_theme_tokens(
            sheet, {"TEXT_MAIN": "#123456"})
        self.assertIn("#123456", rendered)
        self.assertNotIn("#fff/", rendered)

    def test_same_base_style_is_not_rebuilt_for_colour_only_changes(self):
        with patch.object(theme_qt, "_make_proxy_style", wraps=theme_qt._make_proxy_style) as make:
            theme_qt.apply_theme(self.app, self.palettes["light"])
            theme_qt.apply_theme(self.app, self.palettes["dark"])
        self.assertEqual(make.call_count, 0)

    def test_unrelated_and_palette_backed_roles_skip_global_qss_reset(self):
        old = dict(self.palettes["dark"])
        current = theme_qt.build_qss(old)
        fake_app = SimpleNamespace(
            styleSheet=lambda: current,
            setStyleSheet=Mock(),
        )

        for key in ("CONFLICT_HL_WIN", "TEXT_MAIN", "ACCENT"):
            new = dict(old)
            new[key] = "#123456"
            if key == "ACCENT":
                new["ACCENT_HOV"] = "#234567"
            changed = theme_qt._changed_palette_roles(old, new)
            theme_qt._refresh_application_stylesheet(
                fake_app, old, new, changed)
            fake_app.setStyleSheet.assert_not_called()

        # BTN_SUCCESS is still a literal in global QSS, so it must repolish.
        new = dict(old)
        new["BTN_SUCCESS"] = "#345678"
        changed = theme_qt._changed_palette_roles(old, new)
        theme_qt._refresh_application_stylesheet(fake_app, old, new, changed)
        fake_app.setStyleSheet.assert_called_once()

    def test_palette_backed_qss_repolishes_existing_widgets(self):
        button = QPushButton()
        button.setObjectName("PrimaryButton")
        button.setFixedSize(100, 36)
        button.show()
        self.app.processEvents()
        self.widgets.append(button)

        new = dict(theme_qt.active_palette())
        new["ACCENT"] = "#12e456"
        new["ACCENT_HOV"] = "#23d567"
        theme_qt.apply_theme(self.app, new)
        self.app.processEvents()

        # Sample away from the rounded corner. The application stylesheet uses
        # palette(accent), which must be re-resolved for an existing button.
        actual = button.grab().toImage().pixelColor(10, 18).name()
        self.assertEqual(actual, "#12e456")

    def test_role_scoped_binding_skips_unrelated_changes(self):
        calls: list[str] = []

        class Owner:
            def refresh_theme(self, _palette):
                calls.append("refresh")

        owner = Owner()
        theme_qt.bind_theme(owner, roles={"ACCENT"})
        self.assertEqual(calls, ["refresh"])

        unrelated = dict(theme_qt.active_palette())
        unrelated["CONFLICT_HL_WIN"] = "#123456"
        theme_qt.apply_theme(self.app, unrelated)
        self.assertEqual(calls, ["refresh"])

        relevant = dict(theme_qt.active_palette())
        relevant["ACCENT"] = "#654321"
        theme_qt.apply_theme(self.app, relevant)
        self.assertEqual(calls, ["refresh", "refresh"])

    def test_preview_can_skip_redundant_local_stylesheet_reset(self):
        content = Mock()
        content.styleSheet.return_value = "color:#fff/*@amm-theme:TEXT_MAIN:direct*/;"
        fake = SimpleNamespace(
            _content=content, _updaters=[], _inspector=Mock())
        with patch("gui_qt.theme_preview.build_qpalette", return_value=QPalette()):
            ThemePreviewPanel.refresh(
                fake, self.palettes["dark"], restyle=False)
        content.setStyleSheet.assert_not_called()
        content.setPalette.assert_called_once()

    def test_preview_role_inspector_uses_editor_categories(self):
        preview = ThemePreviewPanel()
        self.widgets.append(preview)
        selected: list[tuple[str, tuple[str, ...]]] = []
        preview.rolesSelected.connect(
            lambda label, roles: selected.append((label, tuple(roles))))

        preview._select_roles(
            "Drag selection outline", ("HIGHLIGHT_DRAG",))

        self.assertFalse(preview._inspector.isHidden())
        self.assertIn("Selection and focus", preview._inspector.text())
        self.assertIn("Drag selection outline (HIGHLIGHT_DRAG)",
                      preview._inspector.text())
        self.assertEqual(
            selected, [("Drag selection outline", ("HIGHLIGHT_DRAG",))])

    def test_feedback_popups_render_opaque_panel_backgrounds(self):
        host = QWidget()
        host.resize(900, 700)
        host.show()
        self.widgets.append(host)

        progress = ProgressPopup(host)
        progress.set_progress(1, 2, "Downloading", title="Nexus Download")
        manager = NotificationManager(host)
        manager.notify("Installing dependency…", "info", sticky=True)
        toast = manager._toasts[0]
        self.app.processEvents()

        expected = self.palettes["dark"]["BG_PANEL"].lower()
        for popup in (progress, toast):
            image = popup.grab().toImage()
            actual = image.pixelColor(8, popup.height() // 2).name()
            self.assertEqual(actual, expected)
            self.assertIsNone(popup.graphicsEffect())
            self.assertFalse(
                popup.testAttribute(Qt.WA_TransparentForMouseEvents))

    def test_bindings_are_immediate_isolated_and_weak(self):
        calls: list[str] = []

        class Owner:
            def refresh_theme(self, _palette):
                calls.append("good")

        class Broken:
            def refresh_theme(self, _palette):
                raise ValueError("bad optional listener")

        owner = Owner()
        broken = Broken()
        with patch("builtins.print"):
            theme_qt.bind_theme(broken)
        theme_qt.bind_theme(owner)
        self.assertEqual(calls, ["good"])
        calls.clear()

        with patch("builtins.print"):
            theme_qt.apply_theme(self.app, self.palettes["light"])
        self.assertEqual(calls, ["good"])
        del broken
        gc.collect()

        oid = id(owner)
        ref = weakref.ref(owner)
        del owner
        gc.collect()
        self.assertIsNone(ref())
        self.assertNotIn(oid, theme_qt._theme_bindings)

        # A Python wrapper may briefly outlive its deleted C++ widget. Such a
        # listener is ignored and cannot stop other listeners from refreshing.
        dead = QWidget()
        theme_qt.bind_theme(dead, lambda widget, _p: widget.update())
        dead.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        theme_qt.apply_theme(self.app, self.palettes["dark"])


class SettingsAndEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_settings_persists_and_applies_theme_without_restart(self):
        saved: list[str] = []
        fake = SimpleNamespace(_safe_save=lambda fn, value: fn(value))
        with patch("gui_qt.settings_view.uc.save_appearance_mode",
                   side_effect=saved.append), \
                patch("gui_qt.theme_qt.apply_theme") as apply:
            SettingsView._on_theme_changed(fake, "light")
        self.assertEqual(saved, ["light"])
        apply.assert_called_once_with(self.app)

    def test_editor_defaults_to_compact_implemented_palette(self):
        palette = real_load_palettes()["dark"]
        simple = theme_editor_groups.simple_grouped_for_palette(palette)
        fine = theme_editor_groups.grouped_for_palette(palette)
        simple_keys = {key for _title, rows in simple for key, _label in rows}
        fine_keys = {key for _title, rows in fine for key, _label in rows}

        self.assertEqual(len(simple_keys), 18)
        self.assertEqual(len(fine_keys), 100)
        self.assertIn("ACCENT", simple_keys)
        self.assertIn("CONFLICT_HL_WIN", fine_keys)
        self.assertEqual(
            theme_editor_groups.role_group("HIGHLIGHT_DRAG"),
            "Selection and focus")
        self.assertEqual(
            theme_editor_groups.role_group("PLUGIN_CYCLE_ERR_BG"),
            "Plugin cycle")
        self.assertEqual(
            theme_editor_groups.role_group("FILE_WIN"), "File conflicts")
        # Legacy/preview-only roles used to imply customisation that no real
        # application screen consumed.
        self.assertNotIn("TAG_FOLDER", fine_keys)
        self.assertNotIn("BTN_DANGER_ALT", fine_keys)

    def test_simple_editor_links_equivalent_roles_and_preserves_light_hover(self):
        light = dict(real_load_palettes()["light"])

        accent = theme_editor_groups.derive_simple(
            "ACCENT", "#8844cc", light)
        for linked in ("LINK_BLUE", "DROPDOWN_ARROW", "SCROLL_ACTIVE",
                       "CHECK_FILL"):
            self.assertEqual(accent[linked], "#8844cc")

        button = theme_editor_groups.derive_simple(
            "BTN_SUCCESS", "#55aa66", light)
        base_lum = theme_editor_groups._luminance(button["BTN_SUCCESS"])
        hover_lum = theme_editor_groups._luminance(
            button["BTN_SUCCESS_HOV"])
        self.assertIsNotNone(base_lum)
        self.assertIsNotNone(hover_lum)
        self.assertLess(hover_lum, base_lum)

        selection = theme_editor_groups.derive_simple(
            "BG_SELECT", "#f0f0f0", light)
        self.assertEqual(selection["TEXT_ON_ACCENT"], "#000000")

    def test_editor_source_and_confirmed_colour_preview_but_cancel_does_not(self):
        combo = Mock()
        combo.findData.return_value = 0
        fake = SimpleNamespace(
            _palettes={"dark": {"ACCENT": "#111111", "TEXT_MAIN": "#eeeeee"},
                       "light": {"ACCENT": "#abcdef"}},
            _names={"dark": "Dark", "light": "Light"},
            _advanced=True,
            _editing_id=None,
            _working={"ACCENT": "#999999"},
            _dirty=True,
            _start_combo=combo,
            _delete_btn=Mock(),
            _save_btn=Mock(),
            _save_as_btn=Mock(),
            _preview=Mock(),
            _apply_working_preview=Mock(),
            _rebuild_body=Mock(),
            _paint_swatch=Mock(),
            tr=lambda text: text,
        )
        with patch("gui_qt.theme_editor_view.ct.is_custom_theme", return_value=False):
            ThemeEditorView._load_theme(fake, "light")
        self.assertEqual(fake._working["ACCENT"], "#abcdef")
        fake._save_as_btn.setVisible.assert_called_once_with(False)
        fake._apply_working_preview.assert_called_once_with()
        fake._preview.refresh.assert_called_once_with(
            fake._working, restyle=False)

        fake._apply_working_preview.reset_mock()
        fake._preview.refresh.reset_mock()
        ThemeEditorView._on_color_picked(fake, "ACCENT", QColor("#123456"))
        self.assertEqual(fake._working["ACCENT"], "#123456")
        fake._apply_working_preview.assert_called_once_with()

        fake._apply_working_preview.reset_mock()
        fake._preview.refresh.reset_mock()
        ThemeEditorView._on_color_picked(fake, "ACCENT", None)
        fake._apply_working_preview.assert_not_called()
        fake._preview.refresh.assert_not_called()

    def test_unsaved_close_restores_persisted_theme_once(self):
        fake = SimpleNamespace(_closing=False)
        with patch("gui_qt.theme_editor_view.apply_theme") as apply:
            ThemeEditorView.tab_closing(fake)
            ThemeEditorView.tab_closing(fake)
        apply.assert_called_once_with(self.app)

    def test_save_as_persists_and_remains_selected_when_editor_closes(self):
        mode = {"value": "dark"}
        custom = {"TEXT_MAIN": "#123456", "BASE_QSTYLE": "Fusion"}
        combo = Mock()
        combo.currentData.return_value = "dark"
        fake = SimpleNamespace(
            _start_combo=combo,
            _palettes={"dark": {"TEXT_MAIN": "#eeeeee"}},
            _names={"dark": "Dark"},
            _working=custom,
            _editing_id=None,
            _dirty=True,
            _closing=False,
            _delete_btn=Mock(),
            _save_btn=Mock(),
            _save_as_btn=Mock(),
            _refresh_start_combo=Mock(),
            _refresh_open_theme_selectors=Mock(),
            tr=lambda text: text,
        )
        loaded = {"dark": fake._palettes["dark"], "custom:violet": custom}
        names = {"dark": "Dark", "custom:violet": "Violet"}
        with patch("gui_qt.theme_editor_view.ct.save_custom_theme",
                   return_value="custom:violet"), \
                patch("gui_qt.theme_editor_view.load_palettes", return_value=loaded), \
                patch("gui_qt.theme_editor_view.load_display_names", return_value=names), \
                patch("gui_qt.theme_editor_view.get_ctk_appearance", return_value="dark"), \
                patch("gui_qt.theme_editor_view.uc.save_appearance_mode",
                      side_effect=lambda value: mode.update(value=value)), \
                patch("gui_qt.theme_editor_view.apply_theme") as apply:
            result = ThemeEditorView._do_save(fake, None, "Violet")
            ThemeEditorView.tab_closing(fake)

        self.assertEqual(result, "custom:violet")
        self.assertEqual(mode["value"], "custom:violet")
        self.assertEqual(apply.call_count, 2)
        apply.assert_any_call(self.app)
        fake._save_as_btn.setVisible.assert_called_once_with(True)
        fake._refresh_open_theme_selectors.assert_called_once_with("custom:violet")

    def test_deleting_active_custom_theme_persists_and_previews_dark_fallback(self):
        mode = {"value": "custom:old"}
        combo = Mock()
        combo.findData.return_value = 0
        fake = SimpleNamespace(
            _editing_id="custom:old",
            _palettes={},
            _names={},
            _advanced=False,
            _working={},
            _dirty=False,
            _start_combo=combo,
            _delete_btn=Mock(),
            _save_btn=Mock(),
            _save_as_btn=Mock(),
            _preview=Mock(),
            _refresh_open_theme_selectors=Mock(),
            _refresh_start_combo=Mock(),
            _apply_working_preview=Mock(),
            _rebuild_body=Mock(),
            tr=lambda text: text,
        )
        fake._load_theme = MethodType(ThemeEditorView._load_theme, fake)
        palettes = {"dark": {"TEXT_MAIN": "#eeeeee"}}
        with patch("gui_qt.theme_editor_view.ct.delete_custom_theme") as delete, \
                patch("gui_qt.theme_editor_view.ct.is_custom_theme", return_value=False), \
                patch("gui_qt.theme_editor_view.uc.get_appearance_mode",
                      side_effect=lambda: mode["value"]), \
                patch("gui_qt.theme_editor_view.uc.save_appearance_mode",
                      side_effect=lambda value: mode.update(value=value)), \
                patch("gui_qt.theme_editor_view.load_palettes", return_value=palettes), \
                patch("gui_qt.theme_editor_view.load_display_names",
                      return_value={"dark": "Dark"}):
            ThemeEditorView._do_delete(fake, "custom:old")

        delete.assert_called_once_with("custom:old")
        self.assertEqual(mode["value"], "dark")
        self.assertEqual(fake._working, palettes["dark"])
        fake._apply_working_preview.assert_called_once_with()


class DetachableLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_close_hook_runs_for_permanent_dismissal(self):
        class Page(QWidget):
            def __init__(self):
                super().__init__()
                self.closes = 0

            def tab_closing(self):
                self.closes += 1

        tabs = DetachableTabWidget()
        page = Page()
        tabs.open_tab(page, "Theme", key="theme")
        tabs.close_tab("theme")
        self.assertEqual(page.closes, 1)

    def test_closing_detached_window_redocks_without_close_hook(self):
        class Page(QWidget):
            def __init__(self):
                super().__init__()
                self.closes = 0

            def tab_closing(self):
                self.closes += 1

        tabs = DetachableTabWidget()
        page = Page()
        tabs.open_tab(page, "Theme", key="theme")
        tabs._detach(tabs.indexOf(page), QPoint(50, 50))
        self.assertTrue(tabs._floats)
        tabs._floats[0].close()
        self.app.processEvents()
        self.assertEqual(page.closes, 0)
        tabs.close_tab("theme")
        self.assertEqual(page.closes, 1)


if __name__ == "__main__":
    unittest.main()
