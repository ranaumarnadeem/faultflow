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
    let
      # Single source of truth for the version (see the top-level VERSION file
      # and faultflow/__init__.py, which reads the same file at runtime).
      version = nixpkgs.lib.fileContents ./VERSION;

      # requirements.txt's runtime deps, PLUS tkinter: faultflow.shell.tcl_bridge
      # imports the stdlib `tkinter` module at import time (its Tcl bridge rides
      # on the bundled _tkinter/Tcl C extension) — verified by actually running
      # the packaged `faultflow shell` and hitting "ModuleNotFoundError: No
      # module named '_tkinter'". Plain pkgs.python3 is built without Tk
      # support; nixpkgs splits it out as a withPackages-selectable module.
      faultflowRuntimePkgs =
        ps: with ps; [
          jsonschema
          rich
          tkinter
        ];

      # Reusable packages, as an overlay so downstream flakes can pull faultflow
      # into their own nixpkgs with `overlays = [ faultflow.overlays.default ]`.
      # The per-system outputs below consume this exact overlay, so the package
      # definitions live in one place only.
      overlay = final: _prev: {
        # Named faultflow-cadical (not `cadical`) on purpose: this pins 1.7.4,
        # and clobbering nixpkgs' `cadical` in a downstream overlay would drag
        # every other cadical consumer back to 1.7.4. See nix/cadical.nix for
        # why the pin exists (nixpkgs' 3.0.0 crashes the SAT tests).
        faultflow-cadical = final.callPackage ./nix/cadical.nix { };

        # pybind11's CMake-config output lives under python3Packages, not as a
        # top-level pkgs.pybind11 attribute in this nixpkgs snapshot.
        faultflow-core = final.callPackage ./nix/faultflow-core.nix {
          inherit version;
          cadical = final.faultflow-cadical;
          pybind11 = final.python3Packages.pybind11;
        };

        faultflow = final.callPackage ./nix/faultflow.nix {
          inherit version;
          faultflow-core = final.faultflow-core;
          python3 = final.python3.withPackages faultflowRuntimePkgs;
        };
      };
    in
    {
      overlays.default = overlay;
    }
    // flake-utils.lib.eachSystem [ "x86_64-linux" ] (
      # aarch64-linux / Darwin are plausible but unverified; kept to one tested
      # platform rather than advertising an unbuilt one. Re-add to this list
      # once actually built + tested there.
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ overlay ];
        };
        inherit (pkgs) lib;
        inherit (pkgs) faultflow faultflow-core;
        cadical = pkgs.faultflow-cadical;

        docs = pkgs.callPackage ./nix/docs.nix { inherit version; };
        # quaigh: intentionally not built — see nix/quaigh.nix. Its
        # rustsat-kissat dependency git-clones and compiles Kissat from a
        # build.rs, which cannot work inside Nix's sandboxed build.

        # Runtime + dev + doc Python deps in one interpreter, shared by the
        # devShell and the python-tests check. requirements.txt +
        # requirements-dev.txt + docs/requirements.txt, mapped 1:1.
        pythonDevEnv = pkgs.python3.withPackages (
          ps:
          (faultflowRuntimePkgs ps)
          ++ (with ps; [
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
          ])
        );
      in
      {
        packages = {
          inherit faultflow faultflow-core docs;
          default = faultflow;
        };

        apps.default = {
          type = "app";
          program = "${faultflow}/bin/faultflow";
          meta.description = "Run faultflow (see docs/user_guide/cli_reference.md for subcommands)";
        };

        # `nix fmt` — format every Nix file with nixfmt (the RFC-style
        # formatter). Wrapped so the entrypoint walks the tree; a bare nixfmt
        # package reads stdin and doesn't recurse.
        formatter = pkgs.writeShellApplication {
          name = "faultflow-fmt";
          runtimeInputs = [ pkgs.nixfmt ];
          text = ''
            find . -type f -name '*.nix' -not -path './.git/*' -print0 \
              | xargs -0 -r nixfmt
          '';
        };

        checks = {
          # doCheck=true in nix/faultflow-core.nix — 201/201 Catch2 tests
          # pass fully hermetically (confirmed via this exact derivation,
          # not nix develop). Building it here is the C++ test gate.
          inherit faultflow-core;

          python-tests = pkgs.stdenv.mkDerivation {
            pname = "faultflow-python-tests";
            inherit version;

            src = lib.fileset.toSource {
              root = ./.;
              fileset = lib.fileset.unions [
                ./ff.py
                ./faultflow
                ./VERSION
                ./tests/python
                ./tests/fixtures
                # tests/cpp/fixtures/*.json — several tests/python cases
                # (test_verify_gate.py, test_blackbox_flow.py, ...) load
                # these directly, same tiny synthetic fixtures the Catch2
                # suite uses.
                ./tests/cpp/fixtures
                ./schemas
                ./cells
                ./tests/benchmarks
                # Already git-tracked (see .gitignore's comment) —
                # test_flow_service.py, test_serial_simulation.py, and
                # others read it directly.
                ./config.ofs.example
              ];
            };

            nativeBuildInputs = [
              pythonDevEnv
              pkgs.yosys
              pkgs.iverilog
            ];
            dontConfigure = true;
            dontBuild = true;

            # Same benchmark-regeneration approach as nix/faultflow-core.nix's
            # preCheck (see its comments for the full why), extended with one
            # OSU035 netlist: tests/python/wrap/test_wbr_atpg.py reads
            # tests/benchmarks/iscas85/synth/c17.json (the OSU035-mapped
            # corpus, distinct from synth_sky130/).
            doCheck = true;
            checkPhase = ''
              runHook preCheck

              mkdir -p tests/benchmarks/iscas85/synth_sky130 tests/benchmarks/iscas89/synth_sky130 tests/benchmarks/iscas85/synth
              regen_benchmark() {
                rtl="$1"; top="$2"; outdir="$3"; liberty="$4"
                script="synth_$top.tcl"
                cat > "$script" <<SYNTHEOF
              read_verilog -sv $rtl
              hierarchy -check -top $top
              proc
              flatten
              opt_expr
              opt_clean
              synth -top $top
              dfflibmap -liberty $liberty
              abc -liberty $liberty
              delete t:\$scopeinfo
              clean
              write_json $outdir/$top.json
              write_verilog $outdir/''${top}_synth.v
              SYNTHEOF
                yosys -q -s "$script"
                rm -f "$script"
              }
              sky130_liberty="$PWD/cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"
              osu035_liberty="$PWD/cells/osu/osu035_stdcells.lib"
              regen_benchmark tests/benchmarks/iscas85/c17.v c17 tests/benchmarks/iscas85/synth_sky130 "$sky130_liberty"
              regen_benchmark tests/benchmarks/iscas85/c432.v c432 tests/benchmarks/iscas85/synth_sky130 "$sky130_liberty"
              regen_benchmark tests/benchmarks/iscas85/c499.v c499 tests/benchmarks/iscas85/synth_sky130 "$sky130_liberty"
              regen_benchmark tests/benchmarks/iscas89/s1238.v s1238_bench tests/benchmarks/iscas89/synth_sky130 "$sky130_liberty"
              regen_benchmark tests/benchmarks/iscas85/c17.v c17 tests/benchmarks/iscas85/synth "$osu035_liberty"

              cp ${faultflow-core}/lib/_faultflow_core*.so .
              PYTHONPATH=. pytest tests/python -q

              runHook postCheck
            '';

            installPhase = ''
              mkdir -p "$out"
              touch "$out/ok"
            '';
          };
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
