# The faultflow control plane (ff.py + faultflow/) wrapped into a runnable
# `faultflow` command, with the compiled _faultflow_core extension staged
# alongside ff.py.
#
# faultflow.runner.runner._load_core() (and the ProcessPoolExecutor worker
# init in faultflow.runner.parallel_solve) resolve the compiled extension by
# walking a fixed list of candidate directories relative to `repo_root =
# Path(__file__).resolve().parents[2]` — and repo_root itself is always one
# of those candidates, since it always exists. Python's interpreter also
# auto-prepends a script's own directory to sys.path when run as
# `python3 /path/to/ff.py`. So placing _faultflow_core*.so directly next to
# ff.py makes it importable with no source changes and no PYTHONPATH
# wrapper trick needed.
#
# cells/**/*.json (+ osu035.yml) — the small, faultflow-authored Sky130/
# OSU035 cell maps — ARE git-tracked and are bundled below, so `[design]
# cell_lib = ${result}/share/faultflow/cells/sky130/sky130_fd_sc_hd.json`
# works out of the box. cells/**/*.lib and cells/**/*.v (the large,
# third-party Sky130 PDK Liberty/behavioral-model files, ~94MB) are
# gitignored and NOT bundled — point `[design] liberty` / `verilog_models`
# at your own copy for synthesis/verification, same as before this package
# existed.
{
  lib,
  stdenv,
  # NOTE: `python3` here is expected to be a `python3.withPackages [...]`
  # result (jsonschema, rich, tkinter — see flake.nix's pythonRuntimeEnv),
  # not the bare interpreter. faultflow.shell.tcl_bridge imports stdlib
  # tkinter unconditionally, and plain pkgs.python3 has no Tk support.
  python3,
  makeWrapper,
  yosys,
  iverilog,
  faultflow-core,
}:

stdenv.mkDerivation {
  pname = "faultflow";
  version = "0-unstable";

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../ff.py
      ../faultflow
      ../schemas
      ../cells
    ];
  };

  nativeBuildInputs = [ makeWrapper ];

  dontBuild = true;
  dontConfigure = true;

  installPhase = ''
    runHook preInstall

    mkdir -p "$out/share/faultflow"
    cp ff.py "$out/share/faultflow/"
    cp -r faultflow "$out/share/faultflow/"
    cp -r schemas "$out/share/faultflow/"
    cp -r cells "$out/share/faultflow/"
    cp ${faultflow-core}/lib/_faultflow_core*.so "$out/share/faultflow/"

    makeWrapper ${python3}/bin/python3 "$out/bin/faultflow" \
      --add-flags "$out/share/faultflow/ff.py" \
      --prefix PATH : ${lib.makeBinPath [ yosys iverilog ]}

    runHook postInstall
  '';

  meta = {
    description = "Gate-level stuck-at/transition fault simulator + native SAT ATPG for post-synthesis netlists";
    homepage = "https://github.com/ranaumarnadeem/faultflow";
    license = lib.licenses.asl20;
    mainProgram = "faultflow";
    platforms = lib.platforms.linux;
  };
}
