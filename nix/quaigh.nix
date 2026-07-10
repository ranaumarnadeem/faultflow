# Quaigh (github.com/Coloquinte/quaigh) — optional reference/comparison ATPG,
# used only when `[atpg] tool = quaigh`. Never in the critical path: the
# default is native SAT ATPG (CaDiCaL), and Quaigh receives BENCH only.
#
# NOT packaged here: nl2bench (mapped-Verilog -> BENCH converter). It is not
# part of the quaigh crate/repo (confirmed: quaigh's Cargo.toml defines no
# [[bin]] targets beyond the implicit `quaigh` binary from src/main.rs) and
# its upstream source could not be confirmed. faultflow's own docs already
# describe it as something that "may be present at venv/bin/nl2bench" —
# treat it as a manually-supplied tool until a real upstream is identified.
#
# STATUS: builds up to fetchCrate/cargoHash (both real, verified values below)
# but then FAILS — confirmed by actually attempting the build, not assumed.
# quaigh's transitive dependency rustsat-kissat has a build.rs that does a
# live `git clone https://github.com/arminbiere/kissat.git` and compiles it
# during `cargo build`, which cannot work inside Nix's network-disabled
# sandbox. A real fix means vendoring Kissat's source separately and
# patching rustsat-kissat's build.rs to consume it from a local path instead
# of cloning — more plumbing than this optional, non-critical-path tool
# warrants right now. This derivation is NOT wired into flake.nix's
# packages/devShells as a result. Until fixed, get Quaigh the way faultflow's
# own docs already describe: `cargo install quaigh` outside Nix, somewhere
# with real network access.
{
  lib,
  rustPlatform,
  fetchCrate,
  pkg-config,
  openssl,
}:

rustPlatform.buildRustPackage rec {
  pname = "quaigh";
  version = "0.0.6";

  src = fetchCrate {
    inherit pname version;
    hash = "sha256-0kPmpqg0bwOlOQjkoR9KmHB7pCu8ZdkGBonXESJdHLo=";
  };

  cargoHash = "sha256-PWn60W/HKRCX1Xyzh48ocJXal2GcoGEiuTNK5bs7g0U=";

  # Transitive openssl-sys dep (a benchmark-download helper, unrelated to
  # faultflow's own BENCH-only usage of quaigh) needs pkg-config + real
  # OpenSSL to build against, rather than vendoring/compiling its own copy.
  nativeBuildInputs = [ pkg-config ];
  buildInputs = [ openssl ];

  meta = {
    description = "Logic circuit analysis and optimization, incl. reference ATPG (BENCH-only)";
    homepage = "https://github.com/Coloquinte/quaigh";
    license = with lib.licenses; [
      mit
      asl20
    ]; # Cargo.toml: "MIT OR Apache-2.0"
    mainProgram = "quaigh";
    platforms = lib.platforms.unix;
  };
}
