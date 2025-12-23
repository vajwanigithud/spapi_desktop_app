import json
import sqlite3

from services import db
from services.catalog_images import attach_image_urls, get_image_url_map


def _seed_catalog(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE spapi_catalog (
            asin TEXT PRIMARY KEY,
            title TEXT,
            image TEXT,
            payload TEXT,
            barcode TEXT,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.executemany(
        "INSERT INTO spapi_catalog (asin, image, payload) VALUES (?, ?, ?)",
        [
            ("A1", "https://img/A1.jpg", None),
            (
                "A2",
                "",
                json.dumps(
                    {
                        "images": [
                            {
                                "marketplaceId": "X",
                                "images": [
                                    {"variant": "PT01", "link": "https://img/A2_alt.jpg"},
                                    {"variant": "MAIN", "link": "https://img/A2_main.jpg"},
                                ],
                            }
                        ]
                    }
                ),
            ),
            ("A3", None, None),
        ],
    )
    conn.commit()
    conn.close()


def test_get_image_url_map_filters_empty(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    _seed_catalog(db_path)
    monkeypatch.setattr(db, "CATALOG_DB_PATH", db_path)

    result = get_image_url_map({"A1", "A2", "A3", ""})

    assert result == {
        "A1": "https://img/A1.jpg",
        "A2": "https://img/A2_main.jpg",
    }


def test_attach_image_urls_respects_existing_and_populates_missing(tmp_path, monkeypatch):
    db_path = tmp_path / "catalog.db"
    _seed_catalog(db_path)
    monkeypatch.setattr(db, "CATALOG_DB_PATH", db_path)

    rows = [
        {"asin": "A1"},
        {"asin": "A2", "imageUrl": "existing"},
        {"asin": "A3"},
        {"asin": "MISSING"},
        {"foo": "bar"},
    ]

    result = attach_image_urls(rows)

    assert result[0]["imageUrl"] == "https://img/A1.jpg"
    assert result[1]["imageUrl"] == "existing"  # not overwritten
    assert "imageUrl" not in result[2] or result[2].get("imageUrl") is None
    assert "imageUrl" not in result[3] or result[3].get("imageUrl") is None
    assert "imageUrl" not in result[4]
