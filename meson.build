project(
  'Amethyst-Mod-Manager',
  version: run_command(['src/version.py'], check: true).stdout().strip(),
  license: 'GPL-3.0-or-later',
  license_files: 'LICENSE',
)

application_id = 'io.github.Amethyst.ModManager'

py = import('python').find_installation()

subdir('src')

py.install_sources('Changelog.txt')

install_data(
  'amethyst-mod-manager',
  'amethyst-mod-manager-cli',
  install_mode: 'rwxr-xr-x',
  install_dir: get_option('bindir'),
)

install_data(
  'flatpak' / '@0@.desktop'.format(application_id),
  install_dir: get_option('datadir') / 'applications',
)

install_data(
  'src' / 'appimage' / 'mod-manager.png',
  rename: '@0@.png'.format(application_id),
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

metainfo_conf = configuration_data()
metainfo_conf.set('VERSION', meson.project_version())
date = ''
git = find_program('git', required: false)
if (git.found())
  date = run_command(git, '-C', '.', 'log', '-1', '--format=%cd', '--date=short').stdout().strip()
endif
if (date == '')
  date = run_command(find_program('date'), '+%Y-%m-%d').stdout().strip()
endif
metainfo_conf.set('DATE', date)
metainfo_file = configure_file(
  input: 'flatpak' / '@0@.metainfo.xml.in'.format(application_id),
  output: '@0@.metainfo.xml'.format(application_id),
  configuration: metainfo_conf,
  install_dir: get_option('datadir') / 'metainfo',
)

appstreamcli = find_program('appstreamcli', required: false)
if (appstreamcli.found())
  test(
    'Validate metainfo file',
    appstreamcli,
    args: ['validate', '--no-net', metainfo_file],
    workdir: meson.current_build_dir(),
  )
endif
