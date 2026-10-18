"""Technology profiles (the Tcl shell's ``use_lib_cells``, ``write_netlist -scan
-techmap``) for any library: ``sky130`` and ``osu035`` as before, and
``<pdk>/<library>`` (``sky130A/sky130_fd_sc_hs``) resolved from the PDK root, with
physical scan where the library has a scan flop."""

from __future__ import annotations

from pathlib import Path

import pytest

from faultflow.config import ConfigError, load_config
from faultflow.project.profiles import get_profile, profile_for_config

from .test_resolve import ROOT as PDK_ROOT
from .test_resolve import needs_pdk


@pytest.mark.unit
def test_the_shipped_profiles_are_unchanged() -> None:
    assert get_profile("sky130").supports_physical_scan
    assert not get_profile("osu035").supports_physical_scan
    with pytest.raises(ConfigError, match="<pdk>/<library>"):
        get_profile("tsmc28")


@needs_pdk
@pytest.mark.integration
def test_a_pdk_library_is_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    profile = get_profile("sky130A/sky130_fd_sc_hs", pdk_root=PDK_ROOT)
    assert profile.name == "sky130A/sky130_fd_sc_hs"
    assert profile.cell_map.exists() and profile.liberty.exists()
    assert profile.supports_generic_scan and profile.supports_physical_scan


@needs_pdk
@pytest.mark.integration
def test_a_configs_library_is_its_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, require_cpp_core: None
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ofs = tmp_path / "config.ofs"
    ofs.write_text(
        "[design]\nnetlist = d.v\npdk = sky130A\nstd_cell_library = sky130_fd_sc_ms\n"
        f"pdk_root = {PDK_ROOT}\n",
        encoding="utf-8",
    )
    cfg = load_config(ofs, "top")
    profile = profile_for_config(cfg)
    assert profile.name == "sky130A/sky130_fd_sc_ms"
    assert (profile.cell_map, profile.liberty) == (cfg.cell_lib, cfg.liberty)
    assert cfg.pdk_root == PDK_ROOT
    # The shipped HD config is the sky130 profile.
    plain = tmp_path / "plain.ofs"
    plain.write_text("[design]\nnetlist = d.v\n", encoding="utf-8")
    assert profile_for_config(load_config(plain, "top")).name == "sky130"
