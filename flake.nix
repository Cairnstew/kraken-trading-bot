{
  description = "kraken-trading-bot — package, module, and dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      # ── Packages ──────────────────────────────────────────────────────────
      # Exposes the Python app as `nix build .#kraken-trading-bot`
      # and as the default package.
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          bot-pkg = pkgs.callPackage ./nix/default.nix { };
        in
        {
          default = bot-pkg;
          kraken-trading-bot = bot-pkg;
        }
      );

      # ── NixOS Module ──────────────────────────────────────────────────────
      # Import in your NixOS config:
      #
      #   inputs.kraken-trading-bot.url = "github:Cairnstew/kraken-trading-bot";
      #
      #   imports = [ inputs.kraken-trading-bot.nixosModules.default ];
      #   services.kraken-trading-bot.enable = true;
      #
      nixosModules.default = import ./nix/module.nix;

      # ── Dev Shell ─────────────────────────────────────────────────────────
      # `nix develop` drops you into a shell with Python, requests,
      # websocket-client, and pytest on PATH.
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3.withPackages (ps: with ps; [
            requests
            websocket-client
            python-dotenv
            pytest
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [ python ];
            shellHook = ''
              echo "kraken-trading-bot dev shell"
              python -c 'import kraken_trading_bot; print("kraken_trading_bot:", kraken_trading_bot.__version__)'
              python -c 'import requests, websocket, dotenv; print("deps: requests, websocket-client, python-dotenv")'
            '';
          };
        }
      );
    };
}
