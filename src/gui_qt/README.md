# Qt interface

`app.py` owns the main window and startup. `game_state.py` holds its shared state,
`glue.py` registers backend hooks, and `i18n.py` loads translations.

| Package | Responsibility |
| --- | --- |
| `appearance` | Themes, icons, theme editing and previews |
| `collections`, `nexus`, `thunderstore`, `wabbajack` | Provider browsers and installation interfaces |
| `dialogs` | General overlays and file pickers |
| `downloads` | Download list, locations and archive movement |
| `files` | File tabs, overrides, archive packing and deployment rules |
| `games`, `profiles` | Game configuration and profile management |
| `installers` | FOMOD, BAIN, MPI and installation prompts |
| `launchers`, `wine` | Executable settings, launch handoff and prefixes |
| `modlist`, `plugins`, `saves` | Feature models, views and actions |
| `previews` | Archive, image, audio, video and mesh viewers |
| `runtime` | Workers, callback guards, navigation and startup probes |
| `settings` | Application, wizard and manager-plugin settings |
| `support` | Notifications, updates, logs and help |
| `widgets` | Shared controls, tree headers, menus and card helpers |

Keep feature-specific overlays with their feature. Package initializers stay
minimal so importing a package does not eagerly load its views. Each package has
its own `meson.build`; register new modules there and new packages in the parent
manifest. Preserve Qt class names and explicit translation contexts when moving
code, and check paths derived from `__file__`.
