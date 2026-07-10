# Sphinx-built HTML docs (docs/), matching the toolchain pinned in
# docs/requirements.txt (chosen upstream specifically for clean nixpkgs
# mapping — see docs/getting_started/installation.md).
{
  lib,
  stdenv,
  version,
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
  inherit version;

  # docs/contributing.md is a `{include} ../CONTRIBUTING.md` shim, so the
  # root-level CONTRIBUTING.md must be in the sandbox alongside docs/.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../docs
      ../CONTRIBUTING.md
    ];
  };

  nativeBuildInputs = [ sphinxEnv ];

  dontConfigure = true;

  # -W --keep-going matches .github/workflows/docs.yml: warnings are errors,
  # but collect them all before failing. Keeps the two doc builds in lockstep.
  buildPhase = ''
    runHook preBuild
    sphinx-build -b html -W --keep-going docs "$out"
    runHook postBuild
  '';

  dontInstall = true;

  meta = {
    description = "faultflow documentation (Sphinx/Furo/MyST HTML site)";
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
