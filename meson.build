project(
  'Amethyst-Mod-Manager',
  version: '2.0.4',
  license: 'GPL-3.0-or-later',
  license_files: 'LICENSE',
)

py = import('python').find_installation()

subdir('src')

py.install_sources('Changelog.txt')

install_data(
  'src' / 'cli.py',
  'src' / 'run_qt.py',
  rename: ['amethyst-mod-manager-cli', 'amethyst-mod-manager'],
  install_mode: 'rwxr-xr-x',
  install_dir: get_option('bindir'),
)

install_data(
  'flatpak' / 'io.github.Amethyst.ModManager.desktop',
  install_dir: get_option('datadir') / 'applications',
)

install_data(
  'src' / 'appimage' / 'mod-manager.png',
  rename: 'io.github.Amethyst.ModManager.png',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / '256x256' / 'apps',
)

install_data(
  'LICENSE',
  install_dir: get_option('datadir') / 'licenses' / meson.project_name(),
)

install_data(
  'README.md',
  install_dir: get_option('datadir') / 'doc' / meson.project_name(),
)
