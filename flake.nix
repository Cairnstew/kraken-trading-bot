{
  description = "kraken-trading-bot — package, module, and dev environment";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    kraken-python.url = "github:Cairnstew/kraken-python";
  };

  outputs = { self, nixpkgs, kraken-python }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      # ── Packages ──────────────────────────────────────────────────────────
      # Exposes the Python app as `nix build .#kraken-trading-bot`
      # and as the default package.  The kraken-python API wrapper comes in
      # as a flake input and is built inline as a Python module dependency.
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          bot-pkg = pkgs.callPackage ./nix/default.nix {
            kraken-python-src = kraken-python.outPath;
          };
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
      # `nix develop` drops you into a shell with Python, the kraken-python
      # wrapper, pandas/numpy/gymnasium, and pytest on PATH.
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          kpkg = pkgs.python3.pkgs.buildPythonPackage {
            pname = "kraken-python";
            version = "0.4.0";
            format = "pyproject";
            src = kraken-python.outPath;
            nativeBuildInputs = with pkgs.python3.pkgs; [ setuptools wheel ];
            propagatedBuildInputs = with pkgs.python3.pkgs; [
              requests
              websocket-client
              python-dotenv
            ];
          };
          python = pkgs.python3.withPackages (ps: with ps; [
            kpkg
            python-dotenv
            pytest
            numpy
            pandas
            gymnasium
            pyyaml
            stable-baselines3
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [ python ];
            shellHook = ''
              echo "kraken-trading-bot dev shell"
              python -c 'import kraken_trading_bot; print("kraken_trading_bot:", kraken_trading_bot.__version__)'
              python -c 'import kraken_api, pandas, numpy, gymnasium, yaml; print("deps: kraken-python, pandas, numpy, gymnasium, pyyaml")'
            '';
          };
        }
      );
    };
}