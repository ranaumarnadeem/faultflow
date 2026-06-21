from faultflow.coverage.site_key import (
    SiteProvenance,
    canonical_site_key,
    stem_site_key,
)


def test_stem_site_key() -> None:
    assert stem_site_key(42) == "net:42:stem"


def test_branch_site_key() -> None:
    prov = SiteProvenance(
        yosys_net_id=7,
        kind="branch",
        consumer_instance="u1",
        input_pin="A",
    )
    assert canonical_site_key(prov) == "net:7:branch:u1:A"


def test_pseudo_site_key() -> None:
    prov = SiteProvenance(
        yosys_net_id=0,
        kind="pseudo",
        consumer_instance="u0",
        pseudo_role="ppi",
    )
    assert canonical_site_key(prov) == "pseudo:ppi:u0"
