subdir('Games')
subdir('LOOT')
subdir('Nexus')
subdir('Thunderstore')
subdir('Utils')
subdir('themes')
subdir('gui_qt')
subdir('icons')
subdir('translations')
subdir('wizards_qt')
subdir('wrappers')

py.install_sources(
  'app_bootstrap.py',
  'cli.py',
  'run_qt.py',
  'version.py',
)

# The required stable-ABI Filegraph extension is built into src/ before
# packaging, beside the target-specific LOOT extension.
install_data(
  'amethyst_filegraph.abi3.so',
  install_mode: 'rwxr-xr-x',
  install_dir: py.get_install_dir(),
)
