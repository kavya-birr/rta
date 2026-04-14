from openreversefeed.core.cache import PrewarmCache


def test_cache_stores_all_required_lookups():
    cache = PrewarmCache(
        schemes_by_code={"SCH1": {"id": 1}},
        accounts_by_pan={"ABCDE1234F": {"individual": {"id": "u1"}}},
        folios_by_account_folio_amc={("u1", "F1", 1): {"id": 100}},
    )
    assert cache.schemes_by_code["SCH1"]["id"] == 1
    assert cache.accounts_by_pan["ABCDE1234F"]["individual"]["id"] == "u1"
    assert cache.folios_by_account_folio_amc[("u1", "F1", 1)]["id"] == 100
