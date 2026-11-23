"""Remove a cut netlist's clock port: Fault holds it high and nothing reads it."""

import re
import sys

src, dst, clk = sys.argv[1:]
with open(src, encoding="utf-8") as f:
    text = f.read()
uses = len(re.findall(r"\b%s\b" % re.escape(clk), text))
assert uses == 3, (clk, uses)  # port list, input, wire: nothing else reads it
text = re.sub(r"^\s*%s,\n" % re.escape(clk), "", text, count=1, flags=re.M)
text = re.sub(r"^\s*(input|wire) %s;\n" % re.escape(clk), "", text, flags=re.M)
with open(dst, "w", encoding="utf-8") as f:
    f.write(text)
