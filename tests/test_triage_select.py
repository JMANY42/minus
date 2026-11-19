"""Selection over literal qBittorrent-shaped rows: filtering, ordering, fields."""

from __future__ import annotations

from minus.triage.select import TORRENT_FIELDS, select_torrents

ROWS = [
    {
        "hash": "aaa",
        "name": "Debian ISO",
        "size": 3,
        "progress": 1.0,
        "state": "uploading",
        "content_path": "/d/debian",
        "save_path": "/d",
    },
    {
        "hash": "bbb",
        "name": "ubuntu ISO",
        "size": 1,
        "progress": 0.25,
        "state": "downloading",
        "content_path": "/d/ubuntu",
        "save_path": "/d",
    },
    {
        "hash": "ccc",
        "name": "Alpha Archive",
        "size": 2,
        "progress": 1.0,
        "state": "stalledUP",
        "content_path": "/d/alpha",
        "save_path": "/d",
    },
    {
        "hash": "ddd",
        "name": "beta",
        "size": 4,
        "progress": 1.0,
        "state": "uploading",
        "content_path": "/d/beta",
        "save_path": "/d",
    },
    {
        "hash": "eee",
        "name": "Gamma",
        "size": 5,
        "progress": 0.75,
        "state": "downloading",
        "content_path": "/d/gamma",
        "save_path": "/d",
    },
]


def hashes(rows):
    return [row["hash"] for row in rows]


def test_only_complete_by_default_sorted_by_name_after_progress():
    # Equal progress, so the tie-break is the casefolded name.
    assert hashes(select_torrents(ROWS, "")) == ["ccc", "ddd", "aaa"]


def test_query_is_a_case_insensitive_substring_of_the_name():
    assert hashes(select_torrents(ROWS, "UBUNTU")) == []
    assert hashes(select_torrents(ROWS, "ubuntu", only_complete=False)) == ["bbb"]
    assert hashes(select_torrents(ROWS, "iso")) == ["aaa"]


def test_partial_torrents_are_included_but_ordered_after_complete():
    assert hashes(select_torrents(ROWS, "", only_complete=False)) == [
        "ccc",
        "ddd",
        "aaa",
        "eee",
        "bbb",
    ]


def test_limit_caps_the_result():
    assert hashes(select_torrents(ROWS, "", only_complete=False, limit=2)) == ["ccc", "ddd"]


def test_results_contain_exactly_the_torrent_fields():
    result = select_torrents(ROWS, "debian")[0]

    assert list(result) == list(TORRENT_FIELDS)
    assert result["hash"] == "aaa"
    assert "save_path" not in result


def test_missing_fields_become_none():
    rows = [{"name": "bare", "progress": 1.0}]

    assert select_torrents(rows, "") == [
        {
            "hash": None,
            "name": "bare",
            "size": None,
            "progress": 1.0,
            "state": None,
            "content_path": None,
        }
    ]
