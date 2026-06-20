from faultflow.config import load_config
cfg = load_config("examples/picorv32a_sky130.ofs")
print("tie_xz:", cfg.simulation.tie_xz)
print("unsupported_cells:", cfg.simulation.unsupported_cells)
