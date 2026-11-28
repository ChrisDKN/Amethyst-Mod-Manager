"""Focused self-tests for the GUI-neutral source-link core.

    PYTHONPATH=src python3 -m unittest Utils.mods._source_link_selftest -v
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from Utils.mods.source_link import (
    SourceLinkError, adapters, read_source_link, write_source_link, detect_adapter,
    LinkCandidate, VersionChoice, LinkResult, resolve_link, apply_link,
    verify_choice, identify_installed_sources,
)
from Utils.mods.source_link_stores import (
    ModioAdapter, NexusAdapter, ThunderstoreAdapter, WorkshopAdapter,
)
from Utils.mods.source_link_plugin import PluginProviderAdapter


class SourceLinkMarkerTests(unittest.TestCase):
    def test_write_and_read_source_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            write_source_link(meta, url="https://www.nexusmods.com/skyrimspecialedition/mods/123",
                              provider="nexus", verification="none")
            values = read_source_link(meta)
            self.assertEqual(values["provider"], "nexus")
            self.assertEqual(values["verification"], "none")
            self.assertTrue(values["url"].endswith("/mods/123"))
            self.assertTrue(values.get("linked"))

    def test_write_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[thunderstore]\nnamespace = Team\nname = Mod\n", encoding="utf-8")
            write_source_link(meta, url="https://thunderstore.io/c/x/p/Team/Mod/",
                              provider="thunderstore", verification="manifest")
            text = meta.read_text(encoding="utf-8")
            self.assertIn("[thunderstore]", text)
            self.assertIn("namespace = Team", text)
            self.assertIn("[sourceLink]", text)

    def test_unsupported_link_raises(self):
        with self.assertRaises(SourceLinkError) as ctx:
            detect_adapter("https://example.com/mod/1", game=None)
        self.assertEqual(ctx.exception.code, "unsupported_source")

    def test_write_tolerates_malformed_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[General]\nversion = 1.0\noddkey\n[General]\nmodid = 5\n",
                            encoding="utf-8")
            write_source_link(meta, url="https://www.nexusmods.com/g/mods/1",
                              provider="nexus", verification="none")
            text = meta.read_text(encoding="utf-8")
            self.assertIn("[sourceLink]", text)
            self.assertIn("modid = 5", text)

    def test_non_http_public_links_are_rejected(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        with self.assertRaises(SourceLinkError) as ctx:
            detect_adapter(
                "ftp://nexusmods.com/skyrimspecialedition/mods/123", game)
        self.assertEqual(ctx.exception.code, "unsupported_source")
        self.assertFalse(NexusAdapter().matches(
            "ftp://nexusmods.com/skyrimspecialedition/mods/123", game))
        self.assertFalse(ThunderstoreAdapter().matches(
            "file:///c/x/p/Team/Mod/", game))
        self.assertFalse(ModioAdapter().matches(
            "ftp://mod.io/g/baldursgate3/m/example", game))
        self.assertFalse(WorkshopAdapter().matches(
            "ftp://steamcommunity.com/sharedfiles/filedetails/?id=1", game))

    def test_non_http_plugin_source_is_still_allowed(self):
        provider = {"plugin_id": "p", "provider_id": "d", "label": "P"}
        with patch("Utils.mods.source_link_plugin.providers",
                   return_value=([provider], [])):
            self.assertEqual(
                detect_adapter("example://project/1",
                               SimpleNamespace(game_id="g")).key, "plugin")


class FakeNexusApi:
    def get_mod(self, domain, mod_id):
        return SimpleNamespace(name="Example Mod", summary="Summary", description="Description",
                               author="Author", uploaded_by="Uploader", category_id=7,
                               category_name="Gameplay")

    def get_mod_files(self, domain, mod_id):
        return SimpleNamespace(files=[
            SimpleNamespace(file_id=10, name="Main File", version="1.2.0", mod_version="",
                            category_name="MAIN", uploaded_timestamp=20, file_name="example.zip",
                            size_in_bytes=1024),
            SimpleNamespace(file_id=9, name="Old File", version="1.1.0", mod_version="",
                            category_name="OLD_VERSION", uploaded_timestamp=10, file_name="old.zip",
                            size_in_bytes=500),
        ])


class NexusAdapterTests(unittest.TestCase):
    def test_parse_exact_file_link(self):
        adapter = NexusAdapter()
        candidate = adapter.parse(
            "https://www.nexusmods.com/skyrimspecialedition/mods/123?tab=files&file_id=10",
            game=SimpleNamespace(nexus_game_domains=("skyrimspecialedition",)), api=FakeNexusApi())
        self.assertEqual(candidate.identity["mod_id"], 123)
        self.assertEqual(candidate.identity["file_id"], 10)
        self.assertEqual(candidate.name, "Example Mod")

    def test_versions_preselects_exact_file(self):
        adapter = NexusAdapter()
        candidate = adapter.parse("https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
                                  game=SimpleNamespace(nexus_game_domains=("skyrimspecialedition",)), api=FakeNexusApi())
        choices = adapter.versions(candidate, game=SimpleNamespace(nexus_game_domains=("skyrimspecialedition",)), api=FakeNexusApi())
        self.assertEqual(choices[0].id, "10")
        self.assertTrue(choices[0].recommended)

    def test_stamp_preserves_thunderstore_section(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse("https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
                                  game=game, api=FakeNexusApi())
        choice = adapter.versions(candidate, game=game, api=FakeNexusApi())[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[thunderstore]\nnamespace = Team\n", encoding="utf-8")
            adapter.stamp(meta, candidate, choice)
            text = meta.read_text(encoding="utf-8")
            self.assertIn("modid = 123", text)
            self.assertIn("fileid = 10", text)
            self.assertIn("[thunderstore]", text)
            self.assertIn("namespace = Team", text)

    def test_verify_rejects_bare_version(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse("https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
                                  game=game, api=FakeNexusApi())
        choice = adapter.versions(candidate, game=game, api=FakeNexusApi())[0]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "meta.ini").write_text("[General]\nversion = 1.2.0\n", encoding="utf-8")
            self.assertEqual(adapter.verify(Path(tmp), candidate, choice, game=game, api=FakeNexusApi())[0], "none")

    def test_verify_accepts_exact_identity(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse("https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
                                  game=game, api=FakeNexusApi())
        choice = adapter.versions(candidate, game=game, api=FakeNexusApi())[0]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "meta.ini").write_text(
                "[General]\ngameName = skyrimspecialedition\nmodid = 123\nfileid = 10\n",
                encoding="utf-8")
            self.assertEqual(adapter.verify(Path(tmp), candidate, choice, game=game, api=FakeNexusApi())[0], "metadata")

    def test_wrong_game_domain_rejected(self):
        game = SimpleNamespace(nexus_game_domains=("fallout4",))
        with self.assertRaises(SourceLinkError) as ctx:
            NexusAdapter().parse("https://www.nexusmods.com/skyrimspecialedition/mods/123",
                                 game=game, api=FakeNexusApi())
        self.assertEqual(ctx.exception.code, "wrong_game")

    def test_verify_requires_matching_game_domain(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse(
            "https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
            game=game, api=FakeNexusApi())
        choice = adapter.versions(candidate, game=game, api=FakeNexusApi())[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "right").mkdir()
            (root / "wrong").mkdir()
            (root / "right" / "meta.ini").write_text(
                "[General]\ngameName = skyrimspecialedition\nmodid = 123\nfileid = 10\n",
                encoding="utf-8")
            (root / "wrong" / "meta.ini").write_text(
                "[General]\ngameName = fallout4\nmodid = 123\nfileid = 10\n",
                encoding="utf-8")
            self.assertEqual(
                adapter.verify(root / "right", candidate, choice,
                               game=game, api=FakeNexusApi())[0], "metadata")
            self.assertEqual(
                adapter.verify(root / "wrong", candidate, choice,
                               game=game, api=FakeNexusApi())[0], "none")

    def test_stamp_and_verify_use_selected_file_not_url_file(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse(
            "https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
            game=game, api=FakeNexusApi())
        choices = adapter.versions(candidate, game=game, api=FakeNexusApi())
        selected = choices[1]  # Old File, file_id 9 - not the URL's file_id 10
        self.assertEqual(selected.payload["file_id"], 9)
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            adapter.stamp(meta, candidate, selected)
            self.assertIn("fileid = 9", meta.read_text(encoding="utf-8"))
            self.assertEqual(
                adapter.verify(Path(tmp), candidate, selected,
                               game=game, api=FakeNexusApi())[0], "metadata")
            self.assertEqual(
                adapter.verify(Path(tmp), candidate, choices[0],
                               game=game, api=FakeNexusApi())[0], "none")

    def test_stamp_conflict_on_different_domain_with_same_ids(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse(
            "https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
            game=game, api=FakeNexusApi())
        choice = adapter.versions(candidate, game=game, api=FakeNexusApi())[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text(
                "[General]\ngameName = fallout4\nmodid = 123\nfileid = 10\n",
                encoding="utf-8")
            with self.assertRaises(SourceLinkError) as ctx:
                adapter.stamp(meta, candidate, choice)
            self.assertEqual(ctx.exception.code, "source_conflict")
            self.assertEqual(ctx.exception.details["existing"]["domain"], "fallout4")
            self.assertEqual(ctx.exception.details["new"]["domain"], "skyrimspecialedition")
            adapter.stamp(meta, candidate, choice, confirm_replace=True)

    def test_parse_accepts_mixed_case_domain(self):
        adapter = NexusAdapter()
        candidate = adapter.parse(
            "https://www.nexusmods.com/SkyrimSpecialEdition/mods/123",
            game=SimpleNamespace(nexus_game_domains=("skyrimspecialedition",)),
            api=FakeNexusApi())
        self.assertEqual(candidate.identity["domain"], "skyrimspecialedition")
        self.assertTrue(candidate.url.endswith("/skyrimspecialedition/mods/123"))

    def test_stamp_keeps_existing_version_when_file_has_none(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse(
            "https://www.nexusmods.com/skyrimspecialedition/mods/123",
            game=game, api=FakeNexusApi())
        choice = replace(adapter.versions(candidate, game=game,
                                          api=FakeNexusApi())[0], version="")
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[General]\nversion = 1.2.0\n", encoding="utf-8")
            adapter.stamp(meta, candidate, choice)
            self.assertIn("version = 1.2.0", meta.read_text(encoding="utf-8"))

    def test_stamp_treats_fileless_existing_identity_as_no_conflict(self):
        game = SimpleNamespace(nexus_game_domains=("skyrimspecialedition",))
        adapter = NexusAdapter()
        candidate = adapter.parse(
            "https://www.nexusmods.com/skyrimspecialedition/mods/123?file_id=10",
            game=game, api=FakeNexusApi())
        choice = adapter.versions(candidate, game=game, api=FakeNexusApi())[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text(
                "[General]\ngameName = skyrimspecialedition\nmodid = 123\nfileid = 0\n",
                encoding="utf-8")
            adapter.stamp(meta, candidate, choice)
            self.assertIn("fileid = 10", meta.read_text(encoding="utf-8"))


class ThunderstoreAdapterTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("Utils.mods.source_link_stores.fetch_versions",
                        return_value=[{"version_number": "1.2.3", "file_size": 1}])
        self.fetch_versions = patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _game():
        return SimpleNamespace(thunderstore_community="lethal-company", hexium_community="")

    @staticmethod
    def _hex_game():
        return SimpleNamespace(thunderstore_community="lethal-company", hexium_community="lethal-company")

    def test_parse_plain_and_version_links(self):
        game = SimpleNamespace(thunderstore_community="lethal-company", hexium_community="")
        adapter = ThunderstoreAdapter()
        plain = adapter.parse("https://thunderstore.io/c/lethal-company/p/Team/Mod/", game=game)
        exact = adapter.parse("https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        self.assertEqual((plain.identity["namespace"], plain.identity["name"]), ("Team", "Mod"))
        self.assertEqual(exact.identity["version"], "1.2.3")

    def test_wrong_community_rejected(self):
        game = SimpleNamespace(thunderstore_community="lethal-company", hexium_community="")
        with self.assertRaises(SourceLinkError):
            ThunderstoreAdapter().parse("https://thunderstore.io/c/other/p/Team/Mod/", game=game)

    def test_verify_matches_manifest(self):
        game = SimpleNamespace(thunderstore_community="lethal-company", hexium_community="")
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse("https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manifest.json").write_text(
                '{"name": "Mod", "version_number": "1.2.3"}', encoding="utf-8")
            self.assertEqual(adapter.verify(Path(tmp), candidate, choice)[0], "manifest")

    def test_verify_rejects_different_manifest_version(self):
        game = SimpleNamespace(thunderstore_community="lethal-company", hexium_community="")
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse("https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manifest.json").write_text(
                '{"name": "Mod", "version_number": "1.0.0"}', encoding="utf-8")
            self.assertEqual(adapter.verify(Path(tmp), candidate, choice)[0], "none")

    def test_followup_returns_missing_dependencies(self):
        pkg = SimpleNamespace(is_root=False, package_id="Team-Dep")
        game = SimpleNamespace(thunderstore_community="lethal-company", hexium_community="")
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse("https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with patch("Utils.mods.source_link_stores.resolve_dependencies") as resolve, \
             patch("Utils.mods.source_link_stores.filter_already_installed",
                   return_value=([pkg], [])):
            resolve.return_value = SimpleNamespace(packages=[pkg], failed=[])
            missing, _plugin = adapter.followup(
                game, Path("/tmp/profile"), "Mod", candidate, choice)
        self.assertEqual(tuple(missing), (pkg,))

    def test_matches_only_the_expected_hosts(self):
        game, hex_game = self._game(), self._hex_game()
        ts, hexium = ThunderstoreAdapter(), ThunderstoreAdapter("hexium")
        self.assertTrue(ts.matches("https://thunderstore.io/c/lethal-company/p/Team/Mod/", game))
        self.assertFalse(hexium.matches("https://thunderstore.io/c/lethal-company/p/Team/Mod/", game))
        self.assertTrue(hexium.matches("https://lethal-company.hexium.gg/mods/Team/Mod", hex_game))
        self.assertFalse(ts.matches("https://lethal-company.hexium.gg/mods/Team/Mod", hex_game))
        self.assertTrue(hexium.matches("https://other.hexium.gg/mods/Team/Mod", hex_game))

    def test_parse_hexium_links(self):
        game = self._hex_game()
        adapter = ThunderstoreAdapter("hexium")
        plain = adapter.parse("https://lethal-company.hexium.gg/mods/Team/Mod", game=game)
        exact = adapter.parse("https://lethal-company.hexium.gg/mods/Team/Mod/v/1.2.3", game=game)
        short = adapter.parse("https://lethal-company.hexium.gg/mods/Team/Mod/1.2.3/", game=game)
        self.assertEqual(plain.identity, {
            "community": "lethal-company", "namespace": "Team", "name": "Mod",
            "version": None, "source": "hexium"})
        self.assertEqual(exact.identity["version"], "1.2.3")
        self.assertEqual(short.identity["version"], "1.2.3")
        self.assertEqual(plain.provider, "hexium")

    def test_wrong_hexium_community_rejected(self):
        game = self._hex_game()
        with self.assertRaises(SourceLinkError):
            ThunderstoreAdapter("hexium").parse(
                "https://other.hexium.gg/mods/Team/Mod/", game=game)

    def test_versions_uses_source_community_and_exact_version(self):
        game = self._game()
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse(
            "https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        choices = adapter.versions(candidate, game=game)
        self.fetch_versions.assert_called_once_with(
            "Team", "Mod", source="thunderstore", community="lethal-company")
        self.assertEqual(choices[0].id, "Team-Mod-1.2.3")
        self.assertEqual(choices[0].label, "1.2.3")
        self.assertTrue(choices[0].recommended)
        self.assertEqual(choices[0].payload["version_info"]["file_size"], 1)

    def test_versions_sorted_newest_first_and_recommends_latest(self):
        game = self._game()
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse(
            "https://thunderstore.io/c/lethal-company/p/Team/Mod/", game=game)
        self.fetch_versions.return_value = [
            {"version_number": "1.0.0"}, {"version_number": "1001.0.0"},
            {"version_number": "2.5.0"}, {"version_number": "999.0.0"},
        ]
        choices = adapter.versions(candidate, game=game)
        self.assertEqual([c.version for c in choices],
                         ["1001.0.0", "999.0.0", "2.5.0", "1.0.0"])
        self.assertEqual([c.recommended for c in choices],
                         [True, False, False, False])

    def test_versions_recommends_pinned_exact_not_latest(self):
        game = self._game()
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse(
            "https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.0.0/", game=game)
        self.fetch_versions.return_value = [
            {"version_number": "3.0.0"}, {"version_number": "1.0.0"},
        ]
        choices = adapter.versions(candidate, game=game)
        self.assertEqual([c.version for c in choices], ["3.0.0", "1.0.0"])
        self.assertEqual([c.recommended for c in choices], [False, True])

    def test_stamp_writes_native_meta_and_preserves_other_sections(self):
        game = self._game()
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse(
            "https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text(
                "[sourceLink]\nprovider = thunderstore\n\n[General]\nversion = old\n",
                encoding="utf-8")
            adapter.stamp(meta, candidate, choice)
            text = meta.read_text(encoding="utf-8")
            self.assertIn("[thunderstore]", text)
            self.assertIn("namespace = Team", text)
            self.assertIn("fullname = Team-Mod-1.2.3", text)
            self.assertIn("[sourceLink]", text)
            self.assertIn("[General]", text)

    def test_stamp_requires_confirmation_for_different_package(self):
        game = self._game()
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse(
            "https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text(
                "[thunderstore]\nnamespace = Other\nname = Mod\nversion = 1.0.0\n",
                encoding="utf-8")
            with self.assertRaises(SourceLinkError) as ctx:
                adapter.stamp(meta, candidate, choice)
            self.assertEqual(ctx.exception.code, "source_conflict")
            adapter.stamp(meta, candidate, choice, confirm_replace=True)
            self.assertIn("namespace = Team", meta.read_text(encoding="utf-8"))

    def test_stamp_switches_source_with_confirmation(self):
        game = self._hex_game()
        adapter = ThunderstoreAdapter("hexium")
        candidate = adapter.parse(
            "https://lethal-company.hexium.gg/mods/Team/Mod/v/1.2.3", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text(
                "[thunderstore]\nnamespace = Team\nname = Mod\nversion = 1.0.0\n",
                encoding="utf-8")
            with self.assertRaises(SourceLinkError):
                adapter.stamp(meta, candidate, choice)
            adapter.stamp(meta, candidate, choice, confirm_replace=True)
            text = meta.read_text(encoding="utf-8")
            self.assertIn("[hexium]", text)
            self.assertNotIn("[thunderstore]", text)

    def test_verify_none_reports_manifest_version(self):
        game = self._game()
        adapter = ThunderstoreAdapter()
        candidate = adapter.parse(
            "https://thunderstore.io/c/lethal-company/p/Team/Mod/v/1.2.3/", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "manifest.json").write_text(
                '{"name": "Mod", "version_number": "1.0.0"}', encoding="utf-8")
            self.assertEqual(adapter.verify(Path(tmp), candidate, choice), ("none", "1.0.0"))

    def test_both_adapters_are_registered(self):
        game = self._hex_game()
        self.assertEqual(tuple(a.key for a in adapters()),
                         ("nexus", "thunderstore", "hexium", "modio", "workshop", "plugin"))
        self.assertEqual(detect_adapter(
            "https://thunderstore.io/c/lethal-company/p/Team/Mod/", game).key,
            "thunderstore")
        self.assertEqual(detect_adapter(
            "https://lethal-company.hexium.gg/mods/Team/Mod", game).key, "hexium")


class FakeModioApi:
    def __init__(self, *, mod_id=42, slug="example"):
        self.summary = SimpleNamespace(
            mod_id=mod_id, name="Example Mod",
            profile_url=f"https://mod.io/g/baldursgate3/m/{slug}",
            latest_file_id=99, latest_version="2.0.0")
        self.files = [
            SimpleNamespace(file_id=99, version="2.0.0", filename="example-v2.zip"),
            SimpleNamespace(file_id=98, version="1.0.0", filename="example-v1.zip"),
        ]

    def search_mods(self, **kwargs):
        return [self.summary], 1

    def get_mod_files(self, mod_id):
        return self.files


class AmbiguousModioApi(FakeModioApi):
    def search_mods(self, **kwargs):
        return [self.summary, self.summary], 2


class ModioWorkshopAdapterTests(unittest.TestCase):
    def test_modio_rejects_non_bg3_game(self):
        game = SimpleNamespace(game_id="skyrim_se")
        with self.assertRaises(SourceLinkError) as ctx:
            ModioAdapter().parse("https://mod.io/g/baldursgate3/m/example", game=game)
        self.assertEqual(ctx.exception.code, "unsupported_game")

    def test_workshop_parses_item_url(self):
        game = SimpleNamespace(game_id="skyrim", steam_id="72850")
        candidate = WorkshopAdapter().parse(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=123456", game=game)
        self.assertEqual(candidate.identity["item_id"], "123456")
        self.assertEqual(candidate.identity["app_id"], "72850")

    def test_workshop_rejects_unsupported_game(self):
        game = SimpleNamespace(game_id="unknown", steam_id="")
        with self.assertRaises(SourceLinkError):
            WorkshopAdapter().parse("https://steamcommunity.com/sharedfiles/filedetails/?id=1", game=game)

    def test_workshop_stamp_preserves_general_keys(self):
        game = SimpleNamespace(game_id="skyrim", steam_id="72850")
        adapter = WorkshopAdapter()
        candidate = adapter.parse(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=123456", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[General]\nversion = 1.0\nmodid = 9\n", encoding="utf-8")
            adapter.stamp(meta, candidate, choice)
            text = meta.read_text(encoding="utf-8")
            # Native configparser writers lowercase option names; INI keys are
            # case-insensitive, so match the brief's spelling without regard to case.
            self.assertRegex(text, r"(?i)workshopItemId\s*=\s*123456")
            self.assertRegex(text, r"(?i)workshopAppId\s*=\s*72850")
            self.assertIn("version = 1.0", text)
            self.assertIn("modid = 9", text)

    def test_modio_parse_and_versions(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        adapter = ModioAdapter()
        candidate = adapter.parse("https://mod.io/g/baldursgate3/m/example",
                                  game=game, api=FakeModioApi())
        self.assertEqual(candidate.identity, {
            "mod_id": 42, "slug": "example",
            "profile_url": "https://mod.io/g/baldursgate3/m/example"})
        choices = adapter.versions(candidate, game=game)
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[0].id, "99")
        self.assertTrue(choices[0].recommended)
        self.assertIn("·", choices[0].label)
        self.assertEqual(choices[0].payload["file_id"], 99)
        self.assertEqual(choices[0].payload["name"], "example-v2.zip")

    def test_modio_slug_matching_is_case_insensitive(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        candidate = ModioAdapter().parse(
            "https://mod.io/g/baldursgate3/m/EXAMPLE",
            game=game, api=FakeModioApi(slug="Example"))
        self.assertEqual(candidate.identity["mod_id"], 42)

    def test_modio_missing_credentials(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        key = SimpleNamespace(load_modio_credentials=lambda: ("", ""))
        with patch("Utils.mods.source_link_stores.load_modio_module", return_value=key):
            with self.assertRaises(SourceLinkError) as ctx:
                ModioAdapter().parse("https://mod.io/g/baldursgate3/m/example", game=game)
        self.assertEqual(ctx.exception.code, "modio_credentials")

    def test_modio_no_matching_slug_rejected(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        with self.assertRaises(SourceLinkError) as ctx:
            ModioAdapter().parse("https://mod.io/g/baldursgate3/m/example",
                                 game=game, api=FakeModioApi(slug="other"))
        self.assertEqual(ctx.exception.code, "modio_mod_not_found")

    def test_modio_ambiguous_slug_rejected(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        with self.assertRaises(SourceLinkError) as ctx:
            ModioAdapter().parse("https://mod.io/g/baldursgate3/m/example",
                                 game=game, api=AmbiguousModioApi())
        self.assertEqual(ctx.exception.code, "modio_ambiguous")

    def test_modio_stamp_preserves_other_sections_and_conflict(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        adapter = ModioAdapter()
        candidate = adapter.parse("https://mod.io/g/baldursgate3/m/example",
                                  game=game, api=FakeModioApi())
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[General]\nversion = 1.0\nmodid = 9\n", encoding="utf-8")
            adapter.stamp(meta, candidate, choice)
            text = meta.read_text(encoding="utf-8")
            self.assertIn("[modio]", text)
            self.assertRegex(text, r"(?i)modId\s*=\s*42")
            self.assertRegex(text, r"(?i)fileId\s*=\s*99")
            self.assertIn("modid = 9", text)
            self.assertIn("version = 1.0", text)
            other = LinkCandidate(
                provider="modio", url=candidate.url, name="Other",
                identity={**candidate.identity, "mod_id": 43}, details={})
            with self.assertRaises(SourceLinkError) as ctx:
                adapter.stamp(meta, other, choice)
            self.assertEqual(ctx.exception.code, "source_conflict")
            adapter.stamp(meta, other, choice, confirm_replace=True)
            self.assertRegex(meta.read_text(encoding="utf-8"), r"(?i)modId\s*=\s*43")

    def test_modio_replace_resets_has_update(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        adapter = ModioAdapter()
        candidate = adapter.parse("https://mod.io/g/baldursgate3/m/example",
                                  game=game, api=FakeModioApi())
        choice = adapter.versions(candidate, game=game)[0]
        other = LinkCandidate(
            provider="modio", url=candidate.url, name="Other",
            identity={**candidate.identity, "mod_id": 43}, details={})
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[modio]\nmodId = 42\nhasUpdate = true\n", encoding="utf-8")
            adapter.stamp(meta, other, choice, confirm_replace=True)
            text = meta.read_text(encoding="utf-8")
            self.assertRegex(text, r"(?i)modId\s*=\s*43")
            self.assertRegex(text, r"(?i)hasUpdate\s*=\s*false")

    def test_modio_verify(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        adapter = ModioAdapter()
        candidate = adapter.parse("https://mod.io/g/baldursgate3/m/example",
                                  game=game, api=FakeModioApi())
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            with patch("Utils.mods.source_link_stores.read_publish_metadata_from_staging",
                       return_value=(42, "Example Mod", "2.0.0", Path("x.pak"))):
                self.assertEqual(adapter.verify(Path(tmp), candidate, choice),
                                 ("metadata", "2.0.0"))
            with patch("Utils.mods.source_link_stores.read_publish_metadata_from_staging",
                       return_value=(7, "Example Mod", "2.0.0", Path("x.pak"))):
                self.assertEqual(adapter.verify(Path(tmp), candidate, choice)[0], "none")

    def test_modio_verify_compares_numeric_version_prefixes(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        adapter = ModioAdapter()
        candidate = adapter.parse("https://mod.io/g/baldursgate3/m/example",
                                  game=game, api=FakeModioApi())
        choice = adapter.versions(candidate, game=game)[0]  # version "2.0.0"
        with tempfile.TemporaryDirectory() as tmp:
            for decoded in ("2.0.0", "2.0.0.0", "2.0.0.1234"):
                with self.subTest(decoded=decoded):
                    with patch(
                        "Utils.mods.source_link_stores.read_publish_metadata_from_staging",
                        return_value=(42, "Example Mod", decoded, Path("x.pak")),
                    ):
                        self.assertEqual(adapter.verify(Path(tmp), candidate, choice),
                                         ("metadata", decoded))
            for decoded in ("2.0.1", "2.0.1.7", "1.0.0.0"):
                with self.subTest(decoded=decoded):
                    with patch(
                        "Utils.mods.source_link_stores.read_publish_metadata_from_staging",
                        return_value=(42, "Example Mod", decoded, Path("x.pak")),
                    ):
                        self.assertEqual(adapter.verify(Path(tmp), candidate, choice)[0],
                                         "none")
            # Unparseable versions fall back to a mod-id-only match.
            with patch("Utils.mods.source_link_stores.read_publish_metadata_from_staging",
                       return_value=(42, "Example Mod", "v2-final", Path("x.pak"))):
                self.assertEqual(adapter.verify(Path(tmp), candidate, choice),
                                 ("metadata", "v2-final"))

    def test_workshop_matches_and_followup(self):
        game = SimpleNamespace(game_id="skyrim", steam_id="72850")
        adapter = WorkshopAdapter()
        self.assertTrue(adapter.matches(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=123456", game))
        self.assertTrue(adapter.matches(
            "https://www.steamcommunity.com/workshop/filedetails/?id=123456", game))
        self.assertFalse(adapter.matches(
            "https://example.com/sharedfiles/filedetails/?id=123456", game))
        candidate = adapter.parse(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=123456", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        self.assertEqual((choice.id, choice.version, choice.recommended),
                         ("123456", "", True))
        self.assertEqual(choice.label, candidate.url)
        self.assertEqual(adapter.followup(
            game, Path("/tmp/profile"), "Mod", candidate, choice), ((), ()))

    def test_workshop_stamp_conflict_requires_confirmation(self):
        game = SimpleNamespace(game_id="skyrim", steam_id="72850")
        adapter = WorkshopAdapter()
        candidate = adapter.parse(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=123456", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            meta.write_text("[General]\nworkshopItemId = 999\nversion = 1.0\n",
                            encoding="utf-8")
            with self.assertRaises(SourceLinkError) as ctx:
                adapter.stamp(meta, candidate, choice)
            self.assertEqual(ctx.exception.code, "source_conflict")
            adapter.stamp(meta, candidate, choice, confirm_replace=True)
            text = meta.read_text(encoding="utf-8")
            self.assertRegex(text, r"(?i)workshopItemId\s*=\s*123456")
            self.assertIn("version = 1.0", text)

    def test_workshop_verify_reads_stamped_meta(self):
        game = SimpleNamespace(game_id="skyrim", steam_id="72850")
        adapter = WorkshopAdapter()
        candidate = adapter.parse(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=123456", game=game)
        other = adapter.parse(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=654321", game=game)
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            adapter.stamp(Path(tmp) / "meta.ini", candidate, choice)
            self.assertEqual(adapter.verify(Path(tmp), candidate, choice), ("metadata", ""))
            self.assertEqual(adapter.verify(Path(tmp), other, choice)[0], "none")

    def test_workshop_invalid_item_id_wrapped(self):
        game = SimpleNamespace(game_id="skyrim", steam_id="72850")
        with self.assertRaises(SourceLinkError) as ctx:
            WorkshopAdapter().parse(
                "https://steamcommunity.com/sharedfiles/filedetails/", game=game)
        self.assertEqual(ctx.exception.code, "invalid_item_id")

    def test_modio_and_workshop_are_registered(self):
        game = SimpleNamespace(game_id="baldurs_gate_3")
        workshop_game = SimpleNamespace(game_id="skyrim", steam_id="72850")
        self.assertEqual(detect_adapter("https://mod.io/g/baldursgate3/m/example", game).key,
                         "modio")
        self.assertEqual(detect_adapter(
            "https://steamcommunity.com/sharedfiles/filedetails/?id=123456",
            workshop_game).key, "workshop")


class PluginAdapterTests(unittest.TestCase):
    def _provider(self):
        return {"plugin_id": "example", "provider_id": "downloads", "label": "Example",
                "repository": False, "schemes": ["example"], "hosts": []}

    def _plan(self, checksum="a" * 64):
        descriptor = {"identity": "project/1", "size_bytes": 10, "sha256": checksum,
                      "metadata": {"version": "1.0", "name": "Project"}}
        return SimpleNamespace(descriptor=descriptor)

    def _parse(self, provider=None, game=None):
        game = game or SimpleNamespace(game_id="g")
        with patch("Utils.mods.source_link_plugin.providers",
                   return_value=([provider or self._provider()], [])), \
             patch("Utils.mods.source_link_plugin.resolve", return_value=self._plan()):
            return PluginProviderAdapter().parse("example://project/1", game=game), game

    def test_provider_without_checksum_rejected(self):
        with patch("Utils.mods.source_link_plugin.providers", return_value=([self._provider()], [])), \
             patch("Utils.mods.source_link_plugin.resolve", return_value=self._plan("")):
            with self.assertRaises(SourceLinkError) as ctx:
                PluginProviderAdapter().parse("example://project/1", game=SimpleNamespace(game_id="g"))
        self.assertEqual(ctx.exception.code, "plugin_checksum_required")

    def test_exact_source_stamps_provider_record(self):
        plan = self._plan()
        with patch("Utils.mods.source_link_plugin.providers", return_value=([self._provider()], [])), \
             patch("Utils.mods.source_link_plugin.resolve", return_value=plan):
            adapter = PluginProviderAdapter()
            candidate = adapter.parse("example://project/1", game=SimpleNamespace(game_id="g"))
            choice = adapter.versions(candidate, game=SimpleNamespace(game_id="g"))[0]
            with tempfile.TemporaryDirectory() as tmp:
                meta = Path(tmp) / "meta.ini"
                adapter.stamp(meta, candidate, choice)
                text = meta.read_text(encoding="utf-8")
        self.assertIn("[ManagerPluginSource]", text)
        self.assertIn("identity = project/1", text)
        self.assertIn("sha256 = " + ("a" * 64), text)

    def test_followup_reports_missing_provider_dependencies(self):
        absent = [{"artifact": {"file_name": "dep.zip"}, "source": "example://dep"}]
        adapter = PluginProviderAdapter()
        candidate = SimpleNamespace(identity={"plugin_id": "example", "provider_id": "downloads",
                                              "source": "example://project/1"})
        with patch("Utils.mods.source_link_plugin.missing", return_value=absent):
            _thunderstore, plugin_missing = adapter.followup(
                SimpleNamespace(game_id="g"), Path("/tmp/profile"), "Mod", candidate, None)
        self.assertEqual(tuple(plugin_missing), tuple(absent))

    def test_matches_uses_providers_and_tolerates_bad_source(self):
        game = SimpleNamespace(game_id="g")
        with patch("Utils.mods.source_link_plugin.providers",
                   return_value=([self._provider()], [])) as providers:
            self.assertTrue(PluginProviderAdapter().matches("example://project/1", game))
        providers.assert_called_once_with("g", "example://project/1")
        with patch("Utils.mods.source_link_plugin.providers", return_value=([], ["err"])):
            self.assertFalse(PluginProviderAdapter().matches("example://project/1", game))
        with patch("Utils.mods.source_link_plugin.providers", side_effect=ValueError("bad source")):
            self.assertFalse(PluginProviderAdapter().matches("example://project/1", game))

    def test_parse_rejects_unsupported_and_ambiguous(self):
        game = SimpleNamespace(game_id="g")
        with patch("Utils.mods.source_link_plugin.providers", return_value=([], [])):
            with self.assertRaises(SourceLinkError) as ctx:
                PluginProviderAdapter().parse("example://project/1", game=game)
        self.assertEqual(ctx.exception.code, "unsupported_source")
        with patch("Utils.mods.source_link_plugin.providers",
                   return_value=([self._provider(), self._provider()], [])):
            with self.assertRaises(SourceLinkError) as ctx:
                PluginProviderAdapter().parse("example://project/1", game=game)
        self.assertEqual(ctx.exception.code, "plugin_ambiguous")

    def test_parse_carries_identity_keys(self):
        candidate, _game = self._parse()
        self.assertEqual(candidate.identity, {
            "plugin_id": "example", "provider_id": "downloads", "source": "example://project/1",
            "identity": "project/1", "sha256": "a" * 64,
        })

    def test_versions_repository_returns_catalogue_choices(self):
        provider = {**self._provider(), "repository": True}
        candidate, game = self._parse(provider)
        rows = [
            {"id": "v2", "version": "2.0", "label": "Latest", "source": "example://project/2"},
            {"id": "v1", "version": "1.0", "source": "example://project/1"},
        ]
        with patch("Utils.mods.source_link_plugin.lookup", return_value=(object(), object())), \
             patch("Utils.mods.source_link_plugin.callback",
                   return_value={"versions": rows, "recommended": "v2"}) as callback:
            choices = PluginProviderAdapter().versions(candidate, game=game)
        self.assertEqual(len(choices), 2)
        self.assertEqual(choices[0].payload, {"source": "example://project/2", "descriptor": None})
        self.assertEqual((choices[0].id, choices[0].version, choices[0].recommended),
                         ("v2", "2.0", True))
        self.assertEqual(choices[1].label, "1.0")
        self.assertFalse(choices[1].recommended)
        synthetic = callback.call_args.args[4]["installed"]
        self.assertEqual((synthetic["identity"], synthetic["sha256"],
                          synthetic["size_bytes"], synthetic["metadata"]),
                         ("", "", 0, {}))

    def test_versions_repository_failure_falls_back_to_exact_source(self):
        provider = {**self._provider(), "repository": True}
        candidate, game = self._parse(provider)
        with patch("Utils.mods.source_link_plugin.lookup", side_effect=ValueError("no plugin")), \
             patch("Utils.mods.source_link_plugin.callback", side_effect=ValueError("bad repo")):
            choices = PluginProviderAdapter().versions(candidate, game=game)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0].payload["source"], "example://project/1")
        self.assertEqual(choices[0].payload["descriptor"]["sha256"], "a" * 64)
        self.assertTrue(choices[0].recommended)

    def test_stamp_resolves_repository_source_and_requires_checksum(self):
        provider = {**self._provider(), "repository": True}
        candidate, _game = self._parse(provider)
        choice = SimpleNamespace(payload={"source": "example://project/2", "descriptor": None})
        resolved = SimpleNamespace(descriptor={
            "identity": "project/2", "size_bytes": 12, "sha256": "b" * 64,
            "metadata": {"version": "2.0"}})
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            with patch("Utils.mods.source_link_plugin.resolve", return_value=resolved):
                PluginProviderAdapter().stamp(meta, candidate, choice)
            text = meta.read_text(encoding="utf-8")
            self.assertIn("identity = project/2", text)
            self.assertIn("sha256 = " + ("b" * 64), text)
            with patch("Utils.mods.source_link_plugin.resolve", return_value=self._plan("")):
                with self.assertRaises(SourceLinkError) as ctx:
                    PluginProviderAdapter().stamp(meta, candidate, choice)
            self.assertEqual(ctx.exception.code, "plugin_checksum_required")

    def test_stamp_conflict_requires_confirmation(self):
        candidate, game = self._parse()
        adapter = PluginProviderAdapter()
        choice = adapter.versions(candidate, game=game)[0]
        other = SimpleNamespace(payload={"source": "example://other/9", "descriptor": None})
        resolved = SimpleNamespace(descriptor={
            "identity": "other/9", "size_bytes": 3, "sha256": "c" * 64,
            "metadata": {"version": "9"}})
        with tempfile.TemporaryDirectory() as tmp:
            meta = Path(tmp) / "meta.ini"
            adapter.stamp(meta, candidate, choice)
            with patch("Utils.mods.source_link_plugin.resolve", return_value=resolved):
                with self.assertRaises(SourceLinkError) as ctx:
                    adapter.stamp(meta, candidate, other)
                self.assertEqual(ctx.exception.code, "source_conflict")
                adapter.stamp(meta, candidate, other, confirm_replace=True)
            self.assertIn("identity = other/9", meta.read_text(encoding="utf-8"))

    def test_verify_reads_stamped_meta(self):
        candidate, game = self._parse()
        adapter = PluginProviderAdapter()
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            adapter.stamp(Path(tmp) / "meta.ini", candidate, choice)
            self.assertEqual(adapter.verify(Path(tmp), candidate, choice), ("metadata", "1.0"))
            other = SimpleNamespace(identity={**candidate.identity, "identity": "other/9"})
            self.assertEqual(adapter.verify(Path(tmp), other, choice)[0], "none")

    def test_followup_passes_provider_identity_row(self):
        candidate = SimpleNamespace(identity={"plugin_id": "example", "provider_id": "downloads",
                                              "source": "example://project/1"})
        with patch("Utils.mods.source_link_plugin.missing", return_value=[]) as missing:
            result = PluginProviderAdapter().followup(
                SimpleNamespace(game_id="g"), Path("/tmp/profile"), "Mod", candidate, None)
        self.assertEqual(result, ((), ()))
        missing.assert_called_once()
        self.assertEqual(missing.call_args.args[2], [{
            "plugin_id": "example", "provider_id": "downloads", "source": "example://project/1"}])

    def test_provider_discovery_error_becomes_source_link_error(self):
        with patch("Utils.mods.source_link_plugin.providers", side_effect=OSError("plugin dir gone")):
            with self.assertRaises(SourceLinkError) as ctx:
                PluginProviderAdapter().parse(
                    "example://project/1", game=SimpleNamespace(game_id="g"))
        self.assertEqual(ctx.exception.code, "plugin_provider_failed")
        self.assertEqual(ctx.exception.details, {"url": "example://project/1"})
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_native_error_message_redacts_urls_and_tokens(self):
        with patch("Utils.mods.source_link_plugin.providers",
                   return_value=([self._provider()], [])), \
             patch("Utils.mods.source_link_plugin.resolve",
                   side_effect=ValueError(
                       "failed example://host; token=secret123")):
            with self.assertRaises(SourceLinkError) as ctx:
                PluginProviderAdapter().parse(
                    "example://project/1", game=SimpleNamespace(game_id="g"))
        self.assertIn("<source>", ctx.exception.message)
        self.assertIn("<redacted>", ctx.exception.message)
        self.assertNotIn("secret123", ctx.exception.message)
        self.assertNotIn("example://", ctx.exception.message)
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_parse_resolve_error_becomes_source_link_error(self):
        with patch("Utils.mods.source_link_plugin.providers",
                   return_value=([self._provider()], [])), \
             patch("Utils.mods.source_link_plugin.resolve",
                   side_effect=ValueError("provider exploded")):
            with self.assertRaises(SourceLinkError) as ctx:
                PluginProviderAdapter().parse(
                    "example://project/1", game=SimpleNamespace(game_id="g"))
        self.assertEqual(ctx.exception.code, "plugin_resolve_failed")
        self.assertEqual(ctx.exception.details, {
            "plugin_id": "example", "provider_id": "downloads", "source": "example://project/1"})
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_stamp_resolve_error_becomes_source_link_error(self):
        provider = {**self._provider(), "repository": True}
        candidate, _game = self._parse(provider)
        choice = SimpleNamespace(payload={"source": "example://project/2", "descriptor": None})
        with tempfile.TemporaryDirectory() as tmp:
            with patch("Utils.mods.source_link_plugin.resolve",
                       side_effect=ValueError("provider exploded")):
                with self.assertRaises(SourceLinkError) as ctx:
                    PluginProviderAdapter().stamp(Path(tmp) / "meta.ini", candidate, choice)
        self.assertEqual(ctx.exception.code, "plugin_resolve_failed")
        self.assertEqual(ctx.exception.details, {
            "plugin_id": "example", "provider_id": "downloads", "source": "example://project/2"})
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_malformed_installed_metadata_becomes_source_link_error(self):
        candidate, game = self._parse()
        adapter = PluginProviderAdapter()
        choice = adapter.versions(candidate, game=game)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meta.ini").write_text(
                "[ManagerPluginSource]\nplugin_id = example\nprovider_id = downloads\n"
                "identity = project/1\nsha256 = invalid\nsize_bytes = 10\nmetadata = {}\n",
                encoding="utf-8")
            with self.assertRaises(SourceLinkError) as ctx:
                adapter.stamp(root / "meta.ini", candidate, choice)
            self.assertEqual(ctx.exception.code, "plugin_metadata_invalid")
            self.assertEqual(ctx.exception.details, {
                "plugin_id": "example", "provider_id": "downloads",
                "source": "example://project/1"})
            with self.assertRaises(SourceLinkError) as ctx:
                adapter.verify(root, candidate, choice)
            self.assertEqual(ctx.exception.code, "plugin_metadata_invalid")
            self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_dependency_error_becomes_source_link_error(self):
        candidate = SimpleNamespace(identity={"plugin_id": "example", "provider_id": "downloads",
                                              "source": "example://project/1"})
        with patch("Utils.mods.source_link_plugin.missing", side_effect=ValueError("graph failed")):
            with self.assertRaises(SourceLinkError) as ctx:
                PluginProviderAdapter().followup(
                    SimpleNamespace(game_id="g"), Path("/tmp/profile"), "Mod", candidate, None)
        self.assertEqual(ctx.exception.code, "plugin_dependency_failed")
        self.assertEqual(ctx.exception.details, {
            "plugin_id": "example", "provider_id": "downloads", "source": "example://project/1"})
        self.assertIsInstance(ctx.exception.__cause__, ValueError)

    def test_plugin_adapter_is_registered(self):
        self.assertIn("plugin", tuple(a.key for a in adapters()))


class _FakeAdapter:
    key = "fake"

    def __init__(self, missing=(), fail_followup=False, verifications=None):
        self.calls, self.missing, self.fail_followup = [], tuple(missing), fail_followup
        self.verifications = list(verifications or [("none", "")])

    def matches(self, url, game): return True

    def parse(self, url, game, *, api=None):
        return LinkCandidate(provider="fake", url=url, name="Example", identity={}, details={})

    def versions(self, candidate, game, *, api=None):
        return (VersionChoice(id="v1", label="Example 1.0", version="1.0", recommended=True, payload={}),)

    def verify(self, mod_dir, candidate, choice, *, game=None, api=None):
        return self.verifications.pop(0) if len(self.verifications) > 1 else self.verifications[0]

    def stamp(self, meta_path, candidate, choice, *, confirm_replace=False):
        self.calls.append("stamp")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text("[General]\nversion = 1.0\n", encoding="utf-8")

    def followup(self, game, profile, mod_name, candidate, choice, *, api=None, log=None):
        self.calls.append("followup")
        if self.fail_followup:
            raise RuntimeError("offline")
        return self.missing, ()


class _RecordingAdapter(_FakeAdapter):
    """Records the API handed to every hook, keyed like a real provider."""

    def __init__(self, key, *, candidate=None, verifications=None):
        super().__init__(verifications=verifications)
        self.key = key
        self.apis = []
        self._candidate = candidate

    def parse(self, url, game, *, api=None):
        self.apis.append(api)
        return self._candidate or super().parse(url, game, api=api)

    def versions(self, candidate, game, *, api=None):
        self.apis.append(api)
        if self.key == "fake":
            return super().versions(candidate, game, api=api)
        return (
            VersionChoice(id="v1", label="One", version="1.0",
                          recommended=True, payload={}),)

    def verify(self, mod_dir, candidate, choice, *, game=None, api=None):
        self.apis.append(api)
        if choice.id == "v2":
            return ("manifest", "2.0")
        return ("none", "")

    def followup(self, game, profile, mod_name, candidate, choice, *, api=None, log=None):
        self.apis.append(api)
        return super().followup(game, profile, mod_name, candidate, choice,
                                api=api, log=log)


class _ChoiceAwareAdapter(_FakeAdapter):
    def versions(self, candidate, game, *, api=None):
        return (
            VersionChoice(id="v1", label="One", version="1.0", recommended=True, payload={}),
            VersionChoice(id="v2", label="Two", version="2.0", recommended=False, payload={}),
        )

    def verify(self, mod_dir, candidate, choice, *, game=None, api=None):
        return ("manifest", choice.version) if choice.id == "v2" else ("none", "")


class OrchestratorTests(unittest.TestCase):
    def _resolve(self, adapter, tmp):
        with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
            return resolve_link("https://fake.test/mod", SimpleNamespace(),
                                Path(tmp), api=None)

    def test_apply_rejects_stale_meta_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            mod = Path(tmp)
            (mod / "meta.ini").write_text("[General]\nversion = 1.0\n", encoding="utf-8")
            adapter = _FakeAdapter()
            resolved = self._resolve(adapter, tmp)
            (mod / "meta.ini").write_text("[General]\nversion = 2.0\n", encoding="utf-8")
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                with self.assertRaises(SourceLinkError) as ctx:
                    apply_link(SimpleNamespace(), Path(tmp), mod, resolved, "v1")
            self.assertEqual(ctx.exception.code, "stale_source_link")

    def test_apply_writes_marker_and_returns_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _FakeAdapter(missing=("pkg",))
            resolved = self._resolve(adapter, tmp)
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                result = apply_link(SimpleNamespace(), Path(tmp), Path(tmp), resolved, "v1")
            self.assertIn("[sourceLink]", (Path(tmp) / "meta.ini").read_text(encoding="utf-8"))
            self.assertEqual(tuple(result.missing_thunderstore), ("pkg",))

    def test_apply_calls_followup_after_stamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _FakeAdapter()
            resolved = self._resolve(adapter, tmp)
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                apply_link(SimpleNamespace(), Path(tmp), Path(tmp), resolved, "v1")
            self.assertEqual(adapter.calls, ["stamp", "followup"])

    def test_apply_does_not_roll_back_when_followup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _FakeAdapter(fail_followup=True)
            resolved = self._resolve(adapter, tmp)
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                result = apply_link(SimpleNamespace(), Path(tmp), Path(tmp), resolved, "v1")
            self.assertIn("[sourceLink]", (Path(tmp) / "meta.ini").read_text(encoding="utf-8"))
            self.assertIn("offline", result.message)

    def test_apply_reverifies_the_selected_choice(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _FakeAdapter(verifications=[("none", ""), ("manifest", "1.2.3")])
            resolved = self._resolve(adapter, tmp)
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                result = apply_link(SimpleNamespace(), Path(tmp), Path(tmp), resolved, "v1")
            self.assertEqual(result.verification, "manifest")

    def test_non_nexus_adapter_never_receives_injected_api(self):
        nexus_like = object()
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _RecordingAdapter("modio")
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                resolved = resolve_link("https://fake.test/mod",
                                        SimpleNamespace(), Path(tmp),
                                        api=nexus_like)
                apply_link(SimpleNamespace(), Path(tmp), Path(tmp), resolved,
                           "v1", api=nexus_like)
            self.assertTrue(adapter.apis)
            self.assertTrue(all(entry is None for entry in adapter.apis))

    def test_nexus_adapter_receives_injected_api(self):
        nexus_like = object()
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _RecordingAdapter("nexus")
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                resolved = resolve_link("https://fake.test/mod",
                                        SimpleNamespace(), Path(tmp),
                                        api=nexus_like)
                apply_link(SimpleNamespace(), Path(tmp), Path(tmp), resolved,
                           "v1", api=nexus_like)
            self.assertTrue(adapter.apis)
            self.assertTrue(all(entry is nexus_like for entry in adapter.apis))

    def test_verify_choice_reverifies_the_selected_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _ChoiceAwareAdapter()
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                resolved = resolve_link("https://fake.test/mod",
                                        SimpleNamespace(), Path(tmp))
                with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                    self.assertEqual(
                        verify_choice(SimpleNamespace(), Path(tmp), resolved, "v1"),
                        ("none", ""))
                    self.assertEqual(
                        verify_choice(SimpleNamespace(), Path(tmp), resolved, "v2"),
                        ("manifest", "2.0"))
                    result = apply_link(SimpleNamespace(), Path(tmp), Path(tmp),
                                        resolved, "v2")
            self.assertEqual(result.verification, "manifest")
            self.assertEqual(result.version, "2.0")

    def test_apply_result_carries_name_and_community(self):
        candidate = LinkCandidate(
            provider="fake", url="https://fake.test/mod", name="Example",
            identity={"community": "lethal-company"}, details={})
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _RecordingAdapter("fake", candidate=candidate)
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                resolved = resolve_link("https://fake.test/mod",
                                        SimpleNamespace(), Path(tmp))
                with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                    result = apply_link(SimpleNamespace(), Path(tmp), Path(tmp),
                                        resolved, "v1")
            self.assertEqual(result.name, "Example")
            self.assertEqual(result.community, "lethal-company")
            # The short constructor still works for external callers.
            LinkResult("m", "p", "u", "v", "none", "d", (), (), "")

    def test_plugin_marker_url_is_sanitized(self):
        candidate = LinkCandidate(
            provider="plugin", url="example://user?token=secret123",
            name="Example",
            identity={"plugin_id": "example", "provider_id": "downloads",
                      "source": "example://user?token=secret123",
                      "identity": "project/1", "sha256": "a" * 64},
            details={})
        with tempfile.TemporaryDirectory() as tmp:
            adapter = _RecordingAdapter("plugin", candidate=candidate)
            with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                resolved = resolve_link("example://user?token=secret123",
                                        SimpleNamespace(), Path(tmp))
                with patch("Utils.mods.source_link.adapters", return_value=(adapter,)):
                    apply_link(SimpleNamespace(), Path(tmp), Path(tmp), resolved, "v1")
            marked = read_source_link(Path(tmp) / "meta.ini")
            self.assertEqual(marked["provider"], "plugin")
            self.assertEqual(marked["url"], "plugin:example/downloads")
            self.assertNotIn("secret123", (Path(tmp) / "meta.ini").read_text(
                encoding="utf-8"))


class BatchIdentificationTests(unittest.TestCase):
    def _game(self, game_id="skyrim_se"):
        return SimpleNamespace(game_id=game_id, name="Skyrim", thunderstore_community="",
                               hexium_community="")

    def test_thunderstore_manifest_is_stamped(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "Mod").mkdir()
            row = SimpleNamespace(mod_name="Mod", matched=True, source="thunderstore",
                                  package_id="Team-Mod", manifest=SimpleNamespace(version="1.0"))
            game = self._game()
            game.thunderstore_community = "lethal-company"
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[row]):
                rows = identify_installed_sources(game, Path(tmp))
        self.assertEqual(rows[0].provider, "thunderstore")
        self.assertTrue(rows[0].matched)

    def test_no_evidence_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "Mod").mkdir()
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]):
                rows = identify_installed_sources(self._game(), Path(tmp))
        self.assertFalse(rows[0].matched)
        self.assertNotEqual(rows[0].provider, "nexus")

    def test_nexus_is_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "Mod").mkdir()
            (staging / "Mod" / "readme.txt").write_text("x", encoding="utf-8")
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]):
                rows = identify_installed_sources(self._game(), Path(tmp))
        self.assertTrue(all(row.provider != "nexus" for row in rows))

    def test_batch_does_not_run_post_link_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "Mod").mkdir()
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]), \
                 patch("Utils.mods.source_link.apply_link") as apply:
                identify_installed_sources(self._game(), Path(tmp))
        apply.assert_not_called()

    def test_reserved_and_separator_folders_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            for name in ("overwrite", "profiles", "Mods", "MyMod_separator", "Real"):
                (staging / name).mkdir()
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]) as identify:
                rows = identify_installed_sources(self._game(), Path(tmp))
        self.assertEqual([r.mod for r in rows], ["Real"])
        self.assertNotIn("overwrite", [r.mod for r in rows])
        self.assertNotIn("profiles", [r.mod for r in rows])
        # No unconfigured community means no batch identify pass at all.
        identify.assert_not_called()

    def test_workshop_metadata_is_only_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "Mod").mkdir()
            (staging / "Mod" / "meta.ini").write_text(
                "[General]\nworkshopItemId = 123\n", encoding="utf-8")
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]):
                rows = identify_installed_sources(self._game(), Path(tmp))
        row = next(r for r in rows if r.mod == "Mod")
        self.assertEqual(row.provider, "workshop")
        self.assertTrue(row.matched)

    def test_plugin_and_nexus_require_a_manual_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "PluginMod").mkdir()
            (staging / "PluginMod" / "meta.ini").write_text(
                "[ManagerPluginSource]\nplugin_id = example\n", encoding="utf-8")
            (staging / "NexusMod").mkdir()
            (staging / "NexusMod" / "meta.ini").write_text(
                "[General]\nmodid = 5\nfileid = 9\n", encoding="utf-8")
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]):
                rows = identify_installed_sources(self._game(), Path(tmp))
        by_mod = {r.mod: r for r in rows}
        self.assertEqual(by_mod["PluginMod"].reason, "manual link required")
        self.assertEqual(by_mod["NexusMod"].reason, "paste a link")
        self.assertFalse(by_mod["PluginMod"].matched)
        self.assertFalse(by_mod["NexusMod"].matched)

    def test_modio_meta_is_stamped(self):
        meta = SimpleNamespace(mod_id=42)
        modio = SimpleNamespace(
            resolve_modio_meta=Mock(return_value=meta), write_modio_meta=Mock())
        keys = SimpleNamespace(load_modio_credentials=Mock(return_value=("key", "path")))
        def loader(stem):
            return keys if stem == "modio_key" else modio

        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "Mod").mkdir()
            game = self._game("baldurs_gate_3")
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]), \
                 patch("Utils.mods.source_link.load_modio_module", side_effect=loader):
                rows = identify_installed_sources(game, Path(tmp))
        row = next(r for r in rows if r.mod == "Mod")
        self.assertEqual(row.provider, "modio")
        self.assertTrue(row.matched)
        modio.write_modio_meta.assert_called_once()
        self.assertEqual(modio.resolve_modio_meta.call_args.kwargs["api_key"], "key")

    def test_cancel_halts_folder_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "A").mkdir()
            (staging / "B").mkdir()
            state = {"stop": False}

            def progress(message):
                if message == "checking A":
                    state["stop"] = True

            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]):
                rows = identify_installed_sources(
                    self._game(), Path(tmp), progress=progress,
                    cancel=lambda: state["stop"])
        self.assertEqual([r.mod for r in rows], ["A"])

    def test_symlinks_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp)
            (staging / "Real").mkdir()
            try:
                (staging / "Link").symlink_to(staging / "Real", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with patch("Utils.mods.source_link.resolve_target_staging", return_value=staging), \
                 patch("Utils.mods.source_link.identify_all", return_value=[]):
                rows = identify_installed_sources(self._game(), Path(tmp))
        self.assertEqual([r.mod for r in rows], ["Real"])


if __name__ == "__main__":
    unittest.main()
