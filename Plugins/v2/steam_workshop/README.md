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
installs use a compact 640 × 420 guarded operation panel for manager locking and refresh.
Longer item lists scroll within the panel. The browser
stays open in its tab; changing games or profiles updates it. Closing the browser
stops its metadata requests and timers. The manager’s **Download only** setting decides whether archives are kept
without installing.
Otherwise, the plugin installs them through Amethyst's headless backend into
the selected profile. Select a member profile to install into a profile group.
Close the panel to refresh the manager after installation.

Each card has a full-width **View** button matching the Nexus browser, with **Install** and a blue
**Subscribe** button underneath. For the signed-in Workshop account, Steam's
subscription status switches the button to **Unsubscribe** when appropriate.
Status refreshes when browsing, after an operation, and once a minute while
the browser is active. The subscribed-only list also refreshes once a minute,
including requirements subscribed after the original action completes.
Buttons wait for Steam's answer before offering changes.
Unsubscribe removes only the chosen mod and leaves its requirements subscribed.

Choose **Subscribe** on a card, in a mod's details or right-click menu, or select
mods and use **Subscribe selected on Steam**, to let Steam manage them instead.
Requirements are included. Subscription changes start on press without a
confirmation; the progress panel closes automatically on success.
Subscriptions use the saved Workshop login and notify the Steam client; use the
same account in Steam so it can download and update the content. Enable mods
in-game where required. This path does not create Amethyst downloads, staged
mods, or modlist entries and ignores the manager's Download only setting.
Subscriptions belong to the Steam account and apply across Amethyst profiles.
Manage them here or through Steam. Existing Amethyst installs are preserved.
If an action fails or is cancelled, completed subscriptions are retained; check
Steam before retrying because the last request may have completed.

Under **Filters → Steam subscriptions**, enable **Subscribed mods only** to
browse the signed-in account's subscriptions for the selected game, with
pagination. This lists subscriptions beyond the current public browse page.
Tags and content filters remain available. Public text/author/item search,
sorting, and trending controls are disabled while this filter is selected.
Clear advanced filters to return to public browsing.

With a manager that supports `modlist_sections`, the plugin also shows
**Steam Workshop: Subscribed mods** beneath the normal modlist. These rows are
read only: they have no enable checkbox or priority and do not enter the saved
modlist or deployment. They also appear in detached modlists. Right-click a
subscribed mod to unsubscribe, open its Workshop page, or copy its URL.
Right-click the section to refresh subscriptions or sign in. Lists refresh
after Workshop actions and once a minute while the manager is open. Switching
games or accounts clears the previous list before loading the new one.
The existing Workshop browser remains available on managers without this API.

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

Suggested changelog: Showed Workshop subscriptions as read-only modlist entries with subscription context actions.

Suggested commit: Show Workshop subscriptions in read-only modlist sections
