# Category Sorter

Requires Amethyst 2.6.0 or newer, including 2.6.0 prereleases.

Click **Category Sorter** beside **Refresh Modlist**, or open
**Wizards → Plugins → Category Sorter**, then select **Run**. Review the
proposed layout, approve the manual backup, then approve the sort to apply the
change to the selected game and profile. Cancel leaves the layout unchanged.

Before applying a changed layout, the plugin creates a retained manual backup
using the same backup function as the Restore backup menu. It appears there as
**Before Category Sorter** and includes the mod list, plugin load order and
profile state. Sorting stops if backup creation fails or is cancelled. If you
cancel the sort after the backup succeeds, that backup remains available.
An already sorted profile does not create a backup. The standard plugin host
reviews backup creation and layout application separately.

Mods are grouped into alphabetical category separators using their saved Nexus
category. Missing categories go under **Uncategorized**. Mods within a category
retain their current order. Enabled and disabled states are preserved.

If category sorting would reverse a file-conflict relationship, the complete
connected conflict chain moves into **Conflicts**, in its original order.
Moving the whole chain prevents a move into Conflicts from reversing another
relationship. Archive conflicts are also preserved conservatively. The backend
checks conflict order again before saving the layout.

Existing mod groups stay together under their first mod's category. Locked mods
stay in place; locked separator blocks and blocks with custom deployment paths
are retained. Sorting happens independently around these protected entries,
so a profile can receive multiple category or conflict separators, with numeric
suffixes where necessary. Custom routing is never changed.

Ordinary existing separators are replaced by the category layout. Matching
separator names retain their existing settings. The preview lists removed
separators. Category names unsafe for separator paths are cleaned automatically.

This plugin uses the standard Amethyst plugin host and requires the layout and
batch conflict APIs. It does not access Nexus online, deploy the profile, sort
ESP/ESM plugins, or modify files inside mods. Conflict preservation covers the
current Filegraph snapshot; enabling previously disabled mods can introduce
new conflicts, so run the sorter again after such changes.
