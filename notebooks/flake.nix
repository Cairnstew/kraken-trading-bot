# notebooks/flake.nix — Jupyter dev shell for visualizing the RL data pipeline
#
# Drop in here and run `nix develop` (or `nix develop notebooks/` from the
# repo root) for a Python environment that has BOTH the bot's runtime
# dependencies AND the Jupyter stack in ONE interpreter — so the notebook
# kernel (this same python3.withPackages env) can `import pandas`, `import
# gymnasium`, `import kraken_api` and `import kraken_trading_bot` without
# registering a second kernel.
#
# Pinning: nixpkgs is pinned to the SAME revision the parent flake's root
# input uses (flake.lock "nixpkgs_3" -> e94cb15...), so stable-baselines3,
# torch and matplotlib resolve to store paths the repo has already built —
# this shell should come from cache, not compile torch again. The two
# sibling inputs are re-declared with the same URLs as the parent so the
# notebook closure matches the bot's own.
#
# The bot's own source is NOT a flake input here: the root flake already
# owns that tree, and referencing it as `path:..` would copy `.venv` etc.
# into the Nix store. Instead the shellHook points PYTHONPATH at the live
# working tree, which is how the parent's justfile already operates.

{
  description = "Jupyter dev shell for kraken-trading-bot data-pipeline viz";

  inputs = {
    # Same rev as root flake.lock -> nixpkgs_3 (e94cb152ed51bd6e24eb4a41f1460252beb52cd2)
    nixpkgs.url = "github:NixOS/nixpkgs/e94cb152ed51bd6e24eb4a41f1460252beb52cd2";
    kraken-python.url = "github:Cairnstew/kraken-python";
    kraken-market-data.url = "git+https://github.com/Cairnstew/kraken-market-data";
  };

  outputs = { self, nixpkgs, kraken-python, kraken-market-data }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;

      # The same inline kraken-python build the bot package uses
      # (nix/default.nix), so the notebook interpreter can
      # `import kraken_api` from the same closure as the bot.
      kraken-api = python: python.pkgs.buildPythonPackage {
        pname = "kraken-python";
        version = "0.4.0";
        format = "pyproject";
        src = kraken-python;
        nativeBuildInputs = with python.pkgs; [ setuptools wheel ];
        propagatedBuildInputs = with python.pkgs; [ requests websocket-client python-dotenv ];
        doCheck = false;
      };
    in
    {
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # One interpreter for everything: the bot's deps (mirroring the
          # root devShell's withPackages list) plus the Jupyter/plotting
          # stack and the inline kraken-python package.
          python = pkgs.python3.withPackages (ps: [
            # Root devShell closure (flake.nix, same list)
            ps.python-dotenv
            ps.numpy
            ps.pandas
            ps.gymnasium
            ps.pyyaml
            ps.stable-baselines3
            ps.requests
            ps.pyarrow
            # Notebook + plotting
            ps.jupyter
            ps.jupyterlab
            ps.notebook
            ps.ipykernel
            ps.nbformat
            ps.nbconvert
            ps.matplotlib
            ps.seaborn
            (kraken-api pkgs.python3)
          ]);
        in
        {
          default = pkgs.mkShell {
            packages = [
              python
              # Zed editor inside the shell. Zed auto-detects the
              # notebooks/.venv the shellHook creates below as its Python
              # toolchain (preferred over uv/system globals), so language
              # servers, tasks and the terminal all use this flake's
              # 3.14.7 interpreter + deps.
              pkgs.zed-editor
            ];
            shellHook = ''
              echo "kraken-trading-bot Jupyter dev shell (notebooks/)"
              # Reveal the bot library + kraken-market-data to the kernel.
              # `..` assumes you entered the shell from this directory, the
              # normal way to use it (`cd notebooks && nix develop`).
              export PYTHONPATH="$(cd .. && pwd):${kraken-market-data.outPath}:$PYTHONPATH"
              # Zed's toolchain picker prefers a project virtualenv over a
              # global python (e.g. the uv 3.14.5 in ~/.local/share/uv that
              # cannot even run on NixOS). Provision one from THIS shell's
              # interpreter, with --system-site-packages so it sees the full
              # flake closure (pandas, gymnasium, jupyter, kraken_api, ...).
              .venv/bin/python --version >/dev/null 2>&1 || python -m venv --system-site-packages .venv
              python -c 'import kraken_api, market_data, pandas, numpy, gymnasium, matplotlib; print("kernel deps: kraken_api, market_data, pandas, numpy, gymnasium, matplotlib")'
              command -v jupyter-lab && echo "run: jupyter lab"
              command -v zeditor && echo "run: zeditor"
            '';
          };
        }
      );
    };
}