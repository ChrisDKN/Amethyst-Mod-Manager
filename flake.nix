{
  description = "Amethyst Mod Manager - a Linux native mod manager";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forEachSystem = f:
        nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      packages = forEachSystem (pkgs: {
        default = pkgs.callPackage ./nix/package.nix {
          src = self;
          # libloot 0.29.6's Python bindings support Python up to 3.13.
          python3 = pkgs.python313;
          meson = pkgs.meson.override { python3 = pkgs.python313; };
          version = let
            d = self.lastModifiedDate;
          in "2.4.3-unstable-${builtins.substring 0 4 d}-${builtins.substring 4 2 d}-${builtins.substring 6 2 d}";
        };
      });
    };
}
