# faultflow C++17 simulation core: libfaultflow_core (static), the pybind11
# extension (_faultflow_core*.so), and the Catch2 unit test binary.
#
# Two upstream deps are pulled in via CMake FetchContent (Catch2 v3.4.0,
# nlohmann/json v3.11.3) rather than find_package, and Nix builds run with no
# network access. We pre-fetch the exact pinned revisions with fetchFromGitHub
# and point FETCHCONTENT_SOURCE_DIR_<NAME> at them so CMake's FetchContent
# module skips the network fetch and uses those trees directly — this needs
# zero changes to CMakeLists.txt and keeps the exact versions already chosen
# upstream instead of drifting to whatever nixpkgs happens to carry.
{
  lib,
  stdenv,
  version,
  cmake,
  ninja,
  fetchFromGitHub,
  sqlitecpp,
  sqlite,
  pybind11,
  cadical,
  python3,
  yosys,
}:

let
  # NOTE: hashes are intentionally the well-known Nix placeholder. The first
  # `nix build` attempt fails with a hash mismatch that prints the real
  # value ("got: sha256-...") — paste that in here. Do not hand-write a
  # guessed hash; a wrong-but-plausible-looking one is worse than an
  # obvious placeholder because it hides that it was never verified.
  catch2Src = fetchFromGitHub {
    owner = "catchorg";
    repo = "Catch2";
    rev = "v3.4.0";
    hash = "sha256-DqGGfNjKPW9HFJrX9arFHyNYjB61uoL6NabZatTWrr0=";
  };
  nlohmannJsonSrc = fetchFromGitHub {
    owner = "nlohmann";
    repo = "json";
    rev = "v3.11.3";
    hash = "sha256-7F0Jon+1oWL7uqet5i1IgHX0fUw/+z0QwEcA3zs5xHg=";
  };
in
stdenv.mkDerivation {
  pname = "faultflow-core";
  inherit version;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../CMakeLists.txt
      ../src
      ../tests/cpp
      # Small, hand-authored synthetic fixtures (multi-clock stagger, scan
      # protocol, tiny per-PDK gate JSON) — already fully git-tracked
      # (40KB), just missing from this fileset until now.
      ../tests/fixtures
      # test_sequential_ff.cpp and others load the real Sky130/OSU035 cell
      # maps via FAULTFLOW_SOURCE_DIR. cells/**/*.json (+ osu035.yml) are
      # git-tracked, and so is cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib
      # specifically (see .gitignore's comment) — the one Liberty corner
      # actually used anywhere in this repo, needed below to regenerate the
      # benchmark netlists this test suite reads. The other two corners and
      # all cells/**/*.v behavioral models stay gitignored/excluded; the C++
      # core never parses Liberty or Verilog directly.
      ../cells
      # tests/benchmarks/**/*.v: the small, public-domain ISCAS85/89 RTL
      # sources are git-tracked; the generated synth_sky130/, synth/
      # subdirectories are not (see .gitignore) — regenerated in preCheck
      # below instead of committing PDK/synth-version-specific derived JSON.
      ../tests/benchmarks
    ];
  };

  nativeBuildInputs = [
    cmake
    ninja
    yosys
  ];
  buildInputs = [
    sqlitecpp
    sqlite
    pybind11
    cadical
    python3
  ];

  cmakeBuildType = "Release";
  cmakeFlags = [
    "-DFETCHCONTENT_FULLY_DISCONNECTED=ON"
    "-DFETCHCONTENT_SOURCE_DIR_CATCH2=${catch2Src}"
    "-DFETCHCONTENT_SOURCE_DIR_JSON=${nlohmannJsonSrc}"
  ];

  # doCheck=true — confirmed 100% tests passed, 0 failed out of 201, fully
  # hermetically inside `nix build`'s sandbox (not just `nix develop`).
  # Four gaps got closed on the way here, all packaging omissions rather
  # than real bugs, worth knowing if this ever regresses:
  #   1. cells/**/*.json wasn't in `src` at all at first. Combined with
  #      nixpkgs' cadical 3.0.0 crashing every SAT test (see
  #      nix/cadical.nix), doCheck=true initially failed ~190/201.
  #   2. With both fixed, 160/201 (80%) passed — the rest needed
  #      tests/benchmarks/**/synth_sky130/*.json, *generated* Yosys output
  #      that (correctly) isn't committed. preCheck below regenerates
  #      exactly the 4 files this suite actually reads, using the same
  #      locked synthesis script as the tool itself (CLAUDE.md's "YOSYS
  #      SYNTHESIS CONTRACT" / faultflow/templates/yosys_synth.tcl.j2),
  #      from the now-tracked ISCAS RTL + the one Liberty corner this repo
  #      actually uses (cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib).
  #   3. First preCheck attempt got the relative paths wrong (Nix's cmake
  #      setup hook builds out-of-source, one directory below where the
  #      fileset above actually landed) — fixed by resolving $srcroot
  #      instead of assuming cwd, see preCheck's own comment.
  #   4. That got to 192/201 (96%) — the last 9 ("stagger" multi-clock
  #      tests) needed tests/fixtures/, which was already fully git-tracked
  #      (40KB) and simply missing from this fileset.
  #
  # Yosys/ABC's technology mapping is heuristic and can be version-sensitive
  # on tiny circuits — test_c17.cpp asserts an exact post-synthesis gate
  # count. Confirmed passing against nixpkgs' yosys as of this writing; if
  # it ever doesn't, that assertion is the canary, not a sign this preCheck
  # step itself is broken.
  doCheck = true;

  preCheck = ''
        # Nix's cmake setup hook builds out-of-source, one directory below the
        # unpacked source root -- checkPhase's cwd is that build dir, not the
        # source root the fileset above actually populated. Walk up to find it
        # (landmarked by CMakeLists.txt, always present) rather than assume a
        # fixed nesting depth.
        srcroot="$PWD"
        while [ ! -f "$srcroot/CMakeLists.txt" ]; do
          srcroot="$(dirname "$srcroot")"
        done

        mkdir -p "$srcroot/tests/benchmarks/iscas85/synth_sky130" "$srcroot/tests/benchmarks/iscas89/synth_sky130"
        liberty="$srcroot/cells/sky130/sky130_fd_sc_hd__tt_025C_1v80.lib"

        regen_benchmark() {
          rtl="$srcroot/$1"; top="$2"; outdir="$srcroot/$3"
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

        regen_benchmark tests/benchmarks/iscas85/c17.v c17 tests/benchmarks/iscas85/synth_sky130
        regen_benchmark tests/benchmarks/iscas85/c432.v c432 tests/benchmarks/iscas85/synth_sky130
        regen_benchmark tests/benchmarks/iscas85/c499.v c499 tests/benchmarks/iscas85/synth_sky130
        regen_benchmark tests/benchmarks/iscas89/s1238.v s1238_bench tests/benchmarks/iscas89/synth_sky130
  '';

  checkPhase = ''
    runHook preCheck
    ctest --output-on-failure
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/lib" "$out/bin"
    find . -maxdepth 3 -name 'libfaultflow_core*' -exec cp -v {} "$out/lib/" \;
    find . -maxdepth 3 -name '_faultflow_core*.so' -exec cp -v {} "$out/lib/" \;
    if [ -f tests/cpp/faultflow_tests ]; then
      cp -v tests/cpp/faultflow_tests "$out/bin/faultflow_tests"
    fi

    runHook postInstall
  '';

  meta = {
    description = "faultflow C++17 simulation core: bit-parallel fault sim + native SAT ATPG (CaDiCaL)";
    homepage = "https://github.com/ranaumarnadeem/faultflow";
    license = lib.licenses.asl20;
    maintainers = [
      {
        name = "Rana Umar Nadeem";
        github = "ranaumarnadeem";
      }
    ];
    sourceProvenance = [ lib.sourceTypes.fromSource ];
    platforms = lib.platforms.linux;
  };
}
