{
  description = "tailor - convert media to a target file size with ffmpeg";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f system);
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          tailor = pkgs.stdenvNoCC.mkDerivation {
            pname = "tailor";
            version = "1.0.0";
            src = ./.;

            nativeBuildInputs = [ pkgs.makeWrapper ];

            dontBuild = true;
            dontConfigure = true;

            installPhase = ''
              runHook preInstall
              install -Dm755 tailor.py $out/bin/tailor
              wrapProgram $out/bin/tailor \
                --prefix PATH : ${
                  pkgs.lib.makeBinPath [
                    pkgs.ffmpeg
                    pkgs.python3
                  ]
                }
              runHook postInstall
            '';

            meta = with pkgs.lib; {
              description = "Convert any media file to a target file size using ffmpeg two-pass encoding";
              mainProgram = "tailor";
              platforms = platforms.linux;
            };
          };

          default = self.packages.${system}.tailor;
        }
      );

      # Optional: import this module in your home-manager config and set
      # programs.tailor.enable = true; instead of adding the package by hand.
      homeManagerModules.default =
        {
          config,
          lib,
          pkgs,
          ...
        }:
        let
          cfg = config.programs.tailor;
        in
        {
          options.programs.tailor.enable = lib.mkEnableOption "tailor CLI tool";

          config = lib.mkIf cfg.enable {
            home.packages = [ self.packages.${pkgs.system}.default ];
          };
        };
    };
}
