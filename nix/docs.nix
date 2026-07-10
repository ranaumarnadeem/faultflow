# Sphinx-built HTML docs (docs/), matching the toolchain pinned in
# docs/requirements.txt (chosen upstream specifically for clean nixpkgs
# mapping — see docs/getting_started/installation.md).
{
  lib,
  stdenv,
  python3,
}:

let
  sphinxEnv = python3.withPackages (
    ps: with ps; [
      sphinx
      furo
      myst-parser
      sphinx-copybutton
      sphinx-design
    ]
  );
in
stdenv.mkDerivation {
  pname = "faultflow-docs";
  version = "0-unstable";

  src = lib.fileset.toSource {
    root = ../docs;
    fileset = ../docs;
  };

  nativeBuildInputs = [ sphinxEnv ];

  dontConfigure = true;

  buildPhase = ''
    runHook preBuild
    sphinx-build -b html . "$out"
    runHook postBuild
  '';

  dontInstall = true;

  meta = {
    description = "faultflow documentation (Sphinx/Furo/MyST HTML site)";
    homepage = "https://github.com/ranaumarnadeem/faultflow";
    license = lib.licenses.asl20;
  };
}
