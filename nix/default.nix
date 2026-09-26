# nix/default.nix — Nix package for kraken-trading-bot
#
# Builds the Python library + CLI as a single derivation.
# `buildPythonApplication` registers the console_scripts entry point
# from pyproject.toml and wraps the binary with the correct PYTHONPATH.

{ lib
, python3
}:

python3.pkgs.buildPythonApplication {
  pname = "kraken-trading-bot";
  version = "0.1.0";
  format = "pyproject";

  # Clean source: exclude git, cache, venv, and nix from the Nix store.
  src = lib.cleanSourceWith {
    filter = path: type:
      let
        base = builtins.baseNameOf path;
      in
      base != ".git"
      && base != "__pycache__"
      && base != ".pytest_cache"
      && base != ".venv"
      && base != "nix"
      && base != ".env"
      && base != "*.log";
    src = ./..;
  };

  nativeBuildInputs = with python3.pkgs; [
    setuptools
    wheel
  ];

  propagatedBuildInputs = with python3.pkgs; [
    (python3.pkgs.toPythonModule (pkgs.callPackage ./nix/kraken-python.nix { }))
    python-dotenv
  ];

  # Live-API verification requires credentials; skip in the Nix build.
  doCheck = false;

  meta = with lib; {
    description = "A trading bot framework using the kraken-python API wrapper.";
    homepage = "https://github.com/Cairnstew/kraken-trading-bot";
    license = licenses.mit;
    maintainers = [ ];
    mainProgram = "kraken-trading-bot";
  };
}
