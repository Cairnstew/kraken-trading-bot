# nix/default.nix — Nix package for kraken-trading-bot
#
# Builds the Python library + CLI as a single derivation.
# `buildPythonApplication` registers the console_scripts entry point
# from pyproject.toml and wraps the binary with the correct PYTHONPATH.
#
# The kraken-python API wrapper is passed in as a flake input source
# (`kraken-python-src`) and built inline as a Python module so the trading
# bot can import `kraken_api` from the same interpreter.

{ lib
, python3
, kraken-python-src
}:

let
  kraken-python = python3.pkgs.buildPythonPackage {
    pname = "kraken-python";
    version = "0.4.0";
    format = "pyproject";
    src = kraken-python-src;
    nativeBuildInputs = with python3.pkgs; [
      setuptools
      wheel
    ];
    propagatedBuildInputs = with python3.pkgs; [
      requests
      websocket-client
      python-dotenv
    ];
    doCheck = false;
  };
in
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
    kraken-python
    python-dotenv
    numpy
    pandas
    gymnasium
    pyyaml
    stable-baselines3
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