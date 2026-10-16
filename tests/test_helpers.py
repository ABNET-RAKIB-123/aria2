import os

import pytest

import server as s


def test_clean_folder():
    assert s.clean_folder("/a/b/") == "a/b"
    assert s.clean_folder("a/../b") == "a/b"      # ".." dropped as a segment
    assert s.clean_folder("") == ""
    assert s.clean_folder("./x/.") == "x"


def test_check_name():
    assert s.check_name(" file.txt ") == "file.txt"
    for bad in ("", ".", "..", "a/b", "a\x00b"):
        with pytest.raises(s.ApiError):
            s.check_name(bad)


def test_local_path_inside_root():
    assert s.local_path("") == s.ROOT
    assert s.local_path("sub/x") == os.path.join(s.ROOT, "sub", "x")


def test_local_path_rejects_traversal():
    for bad in ("../etc", "../../etc/passwd", "sub/../../etc"):
        with pytest.raises(s.ApiError):
            s.local_path(bad)


def test_fmt_size():
    assert s.fmt_size(0) == "0 B"
    assert s.fmt_size(1023) == "1023 B"
    assert s.fmt_size(1024) == "1.0 KB"
    assert s.fmt_size(1536) == "1.5 KB"


def test_in_window(monkeypatch):
    # Freeze "now" to 10:00:00 UTC -> minute-of-day 600.
    monkeypatch.setattr(s.time, "time", lambda: 10 * 3600)
    assert s.in_window("09:00", "11:00", 0) is True       # 600 in [540, 660)
    assert s.in_window("11:00", "12:00", 0) is False      # 600 not in [660, 720)
    assert s.in_window("23:00", "02:00", 0) is False      # wrap window, 600 outside
    assert s.in_window("05:00", "05:00", 0) is True        # equal -> always on


def test_rss_matches():
    assert s.rss_matches("My Show 1080p", "show 1080p") is True
    assert s.rss_matches("My Show 720p", "show -720p") is False
    assert s.rss_matches("anything", "") is True


def test_parse_feed_rss():
    xml = ("<?xml version='1.0'?>"
           "<rss><channel>"
           "<item><title>Ep 1</title><enclosure url='http://x/a.torrent'/><guid>g1</guid></item>"
           "<item><title>Ep 2</title><link>magnet:?xt=urn:btih:abc</link></item>"
           "</channel></rss>")
    items = s.parse_feed(xml)
    assert len(items) == 2
    assert items[0]["title"] == "Ep 1"
    assert items[0]["url"] == "http://x/a.torrent"
    assert items[1]["url"].startswith("magnet:")


def test_b64_roundtrip():
    raw = b"hello \x00\xff world"
    assert s.b64d(s.b64e(raw)) == raw


def test_sanitize_aria2_options():
    # dir inside ROOT is kept (normalized to a real path under ROOT)
    p = [["u"], {"dir": s.ROOT + "/sub"}]
    s.sanitize_aria2_options("aria2.addUri", p)
    assert p[1]["dir"].startswith(s.ROOT)
    # dir outside ROOT and path-bearing "out" are rejected
    with pytest.raises(s.ApiError):
        s.sanitize_aria2_options("aria2.addUri", [["u"], {"dir": "/etc"}])
    with pytest.raises(s.ApiError):
        s.sanitize_aria2_options("aria2.addUri", [["u"], {"out": "../x"}])
    # read-only methods carry no options index -> no-op
    s.sanitize_aria2_options("aria2.tellActive", [["gid"]])
