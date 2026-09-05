import main


ASIN_NEW = "B0NEW12345"
ASIN_CACHED = "B0CACHE123"


def test_activate_new_po_asins_reactivates_seeds_and_queues_missing(monkeypatch):
    saved_exclusions = []
    reset = []
    seeded = []
    sourced = []
    queued = []

    monkeypatch.setattr(main, "load_catalog_fetcher_exclusions", lambda: {ASIN_NEW, "B0OLD12345"})
    monkeypatch.setattr(main, "save_catalog_fetcher_exclusions", lambda values: saved_exclusions.append(set(values)))
    monkeypatch.setattr(main, "reset_catalog_fetch_attempts", lambda asin, **_: reset.append(asin) or True)
    monkeypatch.setattr(main, "seed_catalog_universe", lambda asins, **_: seeded.extend(asins) or len(asins))
    monkeypatch.setattr(main, "record_catalog_asin_sources", lambda asins, source, **_: sourced.append((set(asins), source)))
    monkeypatch.setattr(main, "spapi_catalog_status", lambda **_: {ASIN_CACHED: {"title": "Ready", "image": "image.jpg"}})
    monkeypatch.setattr(main, "should_fetch_catalog", lambda asin, fetched, **_: not fetched)
    monkeypatch.setattr(main, "_queue_catalog_fetch", lambda _, asin: queued.append(asin) or True)

    result = main._activate_new_po_catalog_asins([ASIN_NEW.lower(), ASIN_CACHED, "bad"])

    assert result == {"discovered": 2, "seeded": 2, "reactivated": 1, "queued": 1}
    assert saved_exclusions == [{"B0OLD12345"}]
    assert reset == [ASIN_NEW]
    assert set(seeded) == {ASIN_NEW, ASIN_CACHED}
    assert sourced == [({ASIN_NEW, ASIN_CACHED}, "vendor_po")]
    assert queued == [ASIN_NEW]


def test_catalog_queue_deduplicates_pending_asin(monkeypatch):
    main._catalog_fetch_inflight.clear()
    main._catalog_fetch_queue.clear()
    monkeypatch.setattr(main, "load_catalog_fetcher_exclusions", lambda: set())
    monkeypatch.setattr(main, "_ensure_catalog_fetch_worker", lambda: None)

    assert main._queue_catalog_fetch(None, ASIN_NEW) is True
    assert main._queue_catalog_fetch(None, ASIN_NEW.lower()) is False
    assert main._catalog_queue_status()["queued"] == 1

    main._catalog_fetch_queue.clear()
    main._catalog_fetch_inflight.clear()


def test_catalog_queue_rejects_excluded_asin(monkeypatch):
    main._catalog_fetch_inflight.clear()
    main._catalog_fetch_queue.clear()
    monkeypatch.setattr(main, "load_catalog_fetcher_exclusions", lambda: {ASIN_NEW})

    assert main._queue_catalog_fetch(None, ASIN_NEW) is False
    assert ASIN_NEW not in main._catalog_fetch_inflight
