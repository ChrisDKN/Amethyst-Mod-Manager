{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  python3,
  rustPlatform,
  cargo,
  rustc,
  qt6,
  zstd,
  lz4,
  makeWrapper,
  src,
  version,
}:

let
  pythonEnv = python3.withPackages (ps: with ps; [
    pyside6
    requests
    py7zr
    pillow
    lz4
    zstandard
    websocket-client
    keyring
    msgpack
    bsdiff4
  ]);
in
stdenv.mkDerivation (finalAttrs: {
  pname = "amethyst-mod-manager";
  inherit version src;

  cargoDeps = rustPlatform.fetchCargoVendor {
    src = "${src}/native/amethyst_filegraph";
    hash = "sha256-PGyUuwwU44/0lHfHEipgi5TxGyXbdDPar/mDqHLFsgM=";
  };

  cargoRoot = "native/amethyst_filegraph";

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
    cargo
    rustc
    rustPlatform.cargoSetupHook
    qt6.wrapQtAppsHook
    makeWrapper
  ];

  buildInputs = [ pythonEnv zstd lz4 qt6.qtbase ];

  enableParallelBuilding = true;

  patchPhase = ''
    patchShebangs native/amethyst_filegraph/build.sh
    patchShebangs src/version.py
  '';

  preConfigure = ''
    export CARGO_BUILD_JOBS="''${NIX_BUILD_CORES:-1}"
    ./native/amethyst_filegraph/build.sh
  '';

  postFixup = ''
    for f in "$out"/bin/*; do
      if [ -f "$f" ] && [ ! -L "$f" ]; then
        sed -i "s|python3|${pythonEnv}/bin/python3|g" "$f"
        wrapProgram "$f" \
          --prefix PYTHONPATH : "${pythonEnv}/${python3.sitePackages}:$out/${python3.sitePackages}" \
          --prefix PATH : "${pythonEnv}/bin"
      fi
    done
  '';

  meta = {
    description = "Universal mod manager written in Python and Qt";
    homepage = "https://github.com/ChrisDKN/Amethyst-Mod-Manager";
    license = lib.licenses.gpl3Only;
    platforms = lib.platforms.linux;
    mainProgram = "amethyst-mod-manager";
  };
})