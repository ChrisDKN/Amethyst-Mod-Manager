{
  description = "Amethyst Mod Manager - Nix flake";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    amethyst-mod-manager = {
      url = "github:ChrisDKN/Amethyst-Mod-Manager";
      flake = false;
    };
  };

  outputs = { self, nixpkgs, amethyst-mod-manager }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forEachSystem = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forEachSystem (pkgs: {
        default = pkgs.callPackage ./nix/package.nix {
          src = amethyst-mod-manager;
          version = "2.4.3-unstable-${builtins.substring 0 8 amethyst-mod-manager.lastModifiedDate}";
        };
      });
    };
}