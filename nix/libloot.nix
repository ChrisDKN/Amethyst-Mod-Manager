{
  lib,
  buildPythonPackage,
  fetchFromGitHub,
  rustPlatform,
}:

buildPythonPackage (finalAttrs: {
  pname = "libloot";
  version = "0.29.6";
  pyproject = true;

  src = fetchFromGitHub {
    owner = "loot";
    repo = "libloot";
    rev = "136f3983c3eec7d377f83a7e7e0b0129aa5c8fe1";
    hash = "sha256-Pz13z0uQfTeo47NJORfZ8n8ucqZdoLVGNIsrf2+OOGA=";
  };

  cargoDeps = rustPlatform.fetchCargoVendor {
    inherit (finalAttrs) src;
    hash = "sha256-IQowGdrol/JFoh+hGfhwoJ2FumkvbuZsp8Xx/V2hFdw=";
  };

  nativeBuildInputs = [
    rustPlatform.cargoSetupHook
    rustPlatform.maturinBuildHook
  ];

  buildAndTestSubdir = "python";
  maturinBuildFlags = [ "--locked" ];
  env.LIBLOOT_REVISION = finalAttrs.src.rev;

  doCheck = false;
  pythonImportsCheck = [ "loot" ];

  meta = {
    description = "Python bindings for LOOT's plugin metadata and sorting library";
    homepage = "https://github.com/loot/libloot";
    license = lib.licenses.gpl3Plus;
    platforms = lib.platforms.linux;
  };
})
