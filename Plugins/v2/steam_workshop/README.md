# Steam Workshop

An optional Workshop browser for Amethyst 2.6.0-beta.2 and later. It works
with the Testing branch, which has Workshop archive installation support but
does not include the browser or its SteamKit helper.

Install this whole folder under `AmethystModManager/Plugins/` and reopen the
Wizards menu, or restart Amethyst. Open **Workshop** from the toolbar or
**Wizards → Plugins → Steam Workshop** after selecting a configured Steam game
and profile. Browse anonymously; sign in to Steam to download files. QR and
Steam Guard sign-in are supported. Your password is not saved.

Workshop opens as a full-size main-window tab, with the same detach and pin
controls as other tabs. Its labelled Steam button appears immediately after the
built-in Nexus/Thunderstore/Wabbajack buttons. Clicking it opens the browser tab
directly, including when the header uses compact icons or a vertical sidebar.
It requires the updated Testing plugin host with the `providers` location and
optional `preferred_size` support for compact confirmation panels, plus
`menu_tab` support for a single browser entry in the Plugins menu.

A background plugin service checks the selected game's Steam Store categories
and, when necessary, its anonymous Workshop listings. The button and browser
menu entry appear only after Workshop support is confirmed. Confirmed results
are cached for 24 hours; failures keep the button hidden and retry after five
minutes. Non-Steam games are hidden without making a network request. No
Workshop helper or binaries are added to Amethyst itself.

The browser includes search, sorting, tags, dates, pagination, details, bulk
selection, and requirement resolution. Sign-in opens a single guarded panel with an embedded login form. The Steam
account buttons share the browser’s wrapping Browse/Trending bar. Downloads and
installs use the guarded operation panel for manager locking and refresh. The browser
stays open in its tab; changing games or profiles updates it. Closing the browser
stops its metadata requests and timers. The manager’s **Download only** setting decides whether archives are kept
without installing.
Otherwise, the plugin installs them through Amethyst's headless backend into
the selected profile. Select a member profile to install into a profile group.
Close the panel to refresh the manager after installation.

Existing mods are preserved by default. If an installation encounters an
existing mod, the manager's normal existing-mod prompt offers Replace, Rename,
or Cancel. There is no separate blanket replacement setting in the plugin.
Archives requiring installer choices remain in Downloads for manual installation.
Completed installs are retained if a later item fails or you cancel. Closing
waits for downloads, sign-in, or installation to stop before returning control
to the manager.

Browser page size and support-cache data live in `data.json` beside `plugin.json`.
Download-only follows the manager’s Settings; old plugin-local download-only
values are ignored. Temporary
`operation.json` and `operation-result.json` files pass selected actions between
the tab and operation panel; expired or mismatched selections are rejected. Steam session
data uses Amethyst's config `workshop/account/` folder, shared with its existing
Workshop installer. Use **Remove saved login** to clear it. Download tools and
temporary files use Amethyst's Workshop cache; completed archives use its
per-game Downloads cache. Uninstalling the plugin leaves those files intact.

The plugin contains its own Linux x86-64 SteamKit helper, Python client and UI;
it never imports the host's `Workshop` package and needs no Workshop binary
installed in Amethyst. The helper is self-contained and does not require an
installed .NET runtime. DepotDownloader downloads separately on first use.

For Linux ARM64, or to rebuild the helper, install the .NET 8 SDK and run:

```sh
bash build-helper.sh aarch64
# or: bash build-helper.sh x86_64
```

The resulting helper is installed in this plugin's `bin/` directory. The script
does not modify Amethyst. SteamKit2 notices are in `SteamKit2-LICENSE.txt` and `THIRD-PARTY.txt`.
The corresponding helper source is in `native/`.

Suggested changelog: Simplified the Workshop toolbar, fixed nested Steam sign-in dialogs, and followed manager download/install preferences.

Suggested commit: Simplify Workshop controls and embed Steam sign-in
