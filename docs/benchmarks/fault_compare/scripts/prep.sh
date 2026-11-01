#!/bin/bash
# Fault synth + cut for every design; ISCAS85 gets a dummy CLK input (Fault needs a clock port).
set -u
cd ~/ffcmp
F=~/ffcmp/fault-result/bin/fault
mkdir -p src2 nl
for c in c432 c499 c880 c1355 c1908 c2670 c3540 c5315; do
  # add "CLK" as the first port + an input declaration after the module header
  python3 - "$c" <<'EOF'
import re, sys
c = sys.argv[1]
t = open(f"src/{c}.v").read()
t = re.sub(r"module\s+%s\s*\(" % c, f"module {c}(CLK, ", t, count=1)
t = re.sub(r"(module %s\(CLK, [^;]*;)" % c, r"\1\n  input CLK;", t, count=1, flags=re.S)
open(f"src2/{c}.v", "w").write(t)
EOF
done
for s in s27 s298 s344 s382 s526 s820 s1238 s1423; do cp src/$s.v src2/$s.v; done
for d in c432 c499 c880 c1355 c1908 c2670 c3540 c5315 s27 s298 s344 s382 s526 s820 s1238 s1423; do
  case $d in c*) top=$d; clk=CLK; rst="";; s*) top=${d}_bench; clk=blif_clk_net; rst="--reset blif_reset_net";; esac
  $F synth -l tech/osu035_stdcells.lib -t $top -o nl/$d.nl.v src2/$d.v > nl/$d.synth.log 2>&1 || { echo "SYNTH FAIL $d"; continue; }
  $F cut -o nl/$d.cut.v --sclConfig tech/config.yml nl/$d.nl.v --clock $clk $rst > nl/$d.cut.log 2>&1 || { echo "CUT FAIL $d"; continue; }
  ncell=$(grep -cE "^  [A-Z][A-Z0-9]+X[0-9]|^  (DFFSR|DFFPOSX1|DFFNEGX1|MUX2X1|XOR2X1|XNOR2X1|FAX1|HAX1|OAI|AOI)" nl/$d.nl.v)
  nff=$(grep -cE "^  (DFFSR|DFFPOSX1|DFFNEGX1)" nl/$d.nl.v)
  echo "$d top=$top cells~$ncell ff=$nff"
done
