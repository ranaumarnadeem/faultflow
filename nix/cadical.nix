# CaDiCaL, pinned to 1.7.4 — NOT nixpkgs' `cadical` (3.0.0 as of this nixpkgs
# snapshot). Verified by actually running the Catch2 suite: 36 SAT/CaDiCaL
# tests aborted with
#   cadical: fatal error: invalid API usage of 'void CaDiCaL::Solver::add(int)':
#   adding literal '-3' with undeclared variable '3' (checking that user
#   variables are declared explicitly failed as both 'factor' and
#   'factorcheck' are enabled)
# CaDiCaL 3.0.0 enables stricter variable-declaration checking by default
# that faultflow's CNF encoder (src/core/atpg/cnf_encoder.cpp) doesn't
# satisfy — it adds literals without an explicit declare-variable call.
# 1.7.4 is what this repo actually runs on: it's the exact version the
# working WSL environment has installed (`dpkg -l | grep cadical` ->
# `1.7.4-1`, from Ubuntu noble's `libcadical-dev`), where the full ATPG
# suite (Phase 3+) has been exercised successfully all along. This is a real
# upstream API/behavior change between 1.7.4 and 3.0.0, not a packaging
# artifact — installation.md's `git clone .../cadical.git` (unpinned, HEAD)
# recipe is equally exposed on a fresh install today; that's a separate,
# pre-existing gap in this repo's own version pinning, not something this
# packaging pass changes. Bump this pin (and re-verify against a real ctest
# run) once the CNF encoder is updated for newer CaDiCaL's stricter API.
{
  lib,
  stdenv,
  fetchFromGitHub,
}:

stdenv.mkDerivation {
  pname = "cadical";
  version = "1.7.4";

  src = fetchFromGitHub {
    owner = "arminbiere";
    repo = "cadical";
    rev = "rel-1.7.4";
    hash = "sha256-4oQYNgnaMAiFalawNNW4km7n/UVjUsQIRJ+6OC4Hzb8=";
  };

  # CaDiCaL's ./configure is a custom script, not GNU autotools — stdenv's
  # default configurePhase would auto-inject --prefix=$out and it doesn't
  # understand that flag ("invalid option '--prefix=...'"). Disable the
  # default phase and call ./configure ourselves, matching
  # docs/getting_started/installation.md's recipe exactly.
  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    ./configure
    make -j "$NIX_BUILD_CORES"
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib" "$out/include" "$out/bin"
    install -m 0644 src/cadical.hpp "$out/include/"
    install -m 0644 build/libcadical.a "$out/lib/"
    install -m 0755 build/cadical "$out/bin/" 2>/dev/null || true
    install -m 0755 build/mobical "$out/bin/" 2>/dev/null || true
    runHook postInstall
  '';

  meta = {
    description = "CaDiCaL SAT solver, pinned to the version this repo actually runs on";
    homepage = "https://github.com/arminbiere/cadical";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
