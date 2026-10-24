# Application Launcher translations

Copy `en.json` to a manager language code such as `de.json` or `pt_BR.json`.
Translate only the values. Keep English keys and placeholders such as `{name}`,
`{account}`, `{version}` and `{prefix_path}` unchanged. Save as UTF-8 JSON.

The plugin follows Amethyst's language setting, including System default.
Restart Amethyst after changing its language. Missing entries fall back from
regional language to base language, then English. No manifest changes or
compilation are required. Only the English source catalogue is supplied initially.

The catalogue covers the plugin's launcher, application settings editor, Steam
shortcut dialogs, and plugin-owned errors. Manager-provided frame-generation,
MangoHud and system dialogs use the manager's own translations. External tool
output and standalone Steam-helper diagnostic logs keep their original text.
Application names, paths, runner names, account names and saved values are never
translated. Keep a separate copy of contributed catalogues when updating plugins.
