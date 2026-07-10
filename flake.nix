{
  description = "faultflow — gate-level stuck-at/transition fault simulator + native SAT ATPG for post-synthesis netlists";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
    }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-linux" ] (
      system:
      let
        pkgs = import nixpkgs { inherit system; };
        inherit (pkgs) lib;

        # Pinned to 1.7.4, NOT nixpkgs' cadical (3.0.0) — see nix/cadical.nix
        # for why: 3.0.0's stricter variable-declaration checking crashes
        # faultflow's CNF encoder, verified by actually running the suite.
        cadical = pkgs.callPackage ./nix/cadical.nix { };

        # pybind11's CMake-config output lives under python3Packages, not as
        # a top-level pkgs.pybind11 attribute in this nixpkgs snapshot.
        faultflow-core = pkgs.callPackage ./nix/faultflow-core.nix {
          inherit cadical;
          pybind11 = pkgs.python3Packages.pybind11;
        };
        docs = pkgs.callPackage ./nix/docs.nix { };
        # quaigh: intentionally not built here — see nix/quaigh.nix. Its
        # rustsat-kissat dependency git-clones and compiles Kissat from a
        # build.rs, which cannot work inside Nix's sandboxed build.

        # requirements.txt's runtime deps, PLUS tkinter: faultflow.shell.tcl_bridge
        # imports the stdlib `tkinter` module at import time (its Tcl bridge rides
        # on the bundled _tkinter/Tcl C extension) — verified by actually running
        # the packaged `faultflow shell` and hitting
        # "ModuleNotFoundError: No module named '_tkinter'". Plain pkgs.python3
        # is built without Tk support; nixpkgs splits it out as a separate
        # withPackages-selectable module (python3Packages.tkinter).
        pythonRuntimePkgs = ps: with ps; [
          jsonschema
          rich
          tkinter
        ];
        pythonRuntimeEnv = pkgs.python3.withPackages pythonRuntimePkgs;

        faultflow = pkgs.callPackage ./nix/faultflow.nix {
          faultflow-core = faultflow-core;
          python3 = pythonRuntimeEnv;
        };

        # Runtime + dev + doc Python deps in one interpreter, shared by the
        # devShell and the python-tests check. requirements.txt +
        # requirements-dev.txt + docs/requirements.txt, mapped 1:1.
        pythonDevEnv = pkgs.python3.withPackages (
          ps:
          (pythonRuntimePkgs ps)
          ++ (
            with ps;
            [
              pytest
              pytest-cov
              black
              flake8
              mypy
              sphinx
              furo
              myst-parser
              sphinx-copybutton
              sphinx-design
            ]
          )
        );
      in
      {
        packages = {
          inherit faultflow-core faultflow docs;
          default = faultflow;
        };

        apps.default = {
          type = "app";
          program = "${faultflow}/bin/faultflow";
          meta.description = "Run faultflow (see docs/user_guide/cli_reference.md for subcommands)";
        };

        # Build-only gate (doCheck=false in nix/faultflow-core.nix — see the
        # comment there). Both the Catch2 suite (ctest) and a real chunk of
        # tests/python (confirmed by actually running it: 151 failed / 429
        # passed / 14 errors, 100% attributable to the same missing cells/)
        # need the real, gitignored cells/sky130/sky130_fd_sc_hd.json, which
        # isn't part of any git-tracked-only Nix source — so neither is a
        # hermetic `nix flake check` gate here. Run both from `nix develop`
        # in a real checkout that already has cells/ populated:
        #   ctest --test-dir build --output-on-failure
        #   PYTHONPATH=. pytest tests/python -q
        checks = {
          inherit faultflow-core;
        };

        devShells.default = pkgs.mkShell {
          packages = [
            # C++ build toolchain
            pkgs.cmake
            pkgs.ninja
            pkgs.gcc
            pkgs.sqlitecpp
            pkgs.sqlite
            pkgs.python3Packages.pybind11
            cadical

            # External EDA tools (subprocess, resolved on PATH)
            pkgs.yosys
            pkgs.iverilog
            pkgs.verilator # not wired as a verify_tool today; kept for exploration
            # Quaigh (optional reference/comparison ATPG) is NOT included here —
            # it cannot build inside Nix's sandbox (see nix/quaigh.nix). Get it
            # with `cargo install quaigh` outside Nix if you need [atpg] tool =
            # quaigh; nl2bench isn't packaged either (see the same file).

            # Python control plane + dev + docs tooling
            pythonDevEnv

            pkgs.git
          ];

          shellHook = ''
            echo "faultflow dev shell"
            echo "  build:  cmake -S . -B build -G Ninja && cmake --build build"
            echo "  test:   ctest --test-dir build --output-on-failure"
            echo "  run:    PYTHONPATH=. python3 ff.py init --top c17 -c config.ofs"
            echo "  docs:   cd docs && sphinx-build -b html . _build/html"
          '';
        };
      }
    );
}
