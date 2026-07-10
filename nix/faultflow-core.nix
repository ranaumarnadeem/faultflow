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
  cmake,
  ninja,
  fetchFromGitHub,
  sqlitecpp,
  sqlite,
  pybind11,
  cadical,
  python3,
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
  version = "0-unstable";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../CMakeLists.txt
      ../src
      ../tests/cpp
      # test_sequential_ff.cpp and others load the real Sky130/OSU035 cell
      # maps via FAULTFLOW_SOURCE_DIR. Only the small, faultflow-authored
      # cells/**/*.json (+ osu035.yml) are git-tracked — cells/**/*.lib and
      # cells/**/*.v (the large, third-party Sky130 PDK Liberty/behavioral
      # files, ~94MB) are gitignored and stay that way; the C++ core never
      # parses Liberty and doesn't need them.
      ../cells
    ];
  };

  nativeBuildInputs = [
    cmake
    ninja
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

  # doCheck stays false — a real, now much narrower gap, not the original
  # bug. History of this line, for whoever touches it next:
  #
  # 1. First cut: cells/**/*.json wasn't in `src` above at all (an omission
  #    in this packaging, not a git-tracking problem — cells/*.json turned
  #    out to already be tracked in HEAD the whole time; .gitignore only
  #    blocks *new* untracked files, it doesn't retroactively drop files
  #    already in the index). Combined with nixpkgs' cadical 3.0.0 crashing
  #    every SAT test (see nix/cadical.nix), doCheck=true failed ~190/201.
  # 2. Fixed both: cells/ added to `src`, CaDiCaL pinned to 1.7.4. Re-ran
  #    doCheck=true hermetically: 160/201 passing (80%), all 41 remaining
  #    failures are e.g. "Cannot open file:
  #    /build/source/tests/benchmarks/iscas85/synth_sky130/c17.json" —
  #    tests/benchmarks/ is *generated* Yosys synthesis output (regenerable
  #    from the tracked ISCAS RTL, e.g. tests/benchmarks/iscas85/c17.v) and
  #    is correctly gitignored as a build artifact, same category as
  #    build/. Pulling it into a hermetic Nix build would mean invoking
  #    Yosys with the right synthesis template per design as part of this
  #    derivation — real scope beyond packaging the tool as it exists.
  # 3. doCheck=true with ANY failing test fails the whole derivation (no
  #    partial credit), so this must stay false or `nix build`/`.#faultflow`
  #    breaks outright for everyone — confirmed 201/201 only ever holds via
  #    `nix develop` in a real checkout, where cells/ AND tests/benchmarks/
  #    both exist natively: `ctest --test-dir build --output-on-failure`.
  doCheck = false;

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
    platforms = lib.platforms.linux;
  };
}
