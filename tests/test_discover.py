from __future__ import annotations

import json

import pytest

from knowledge_base.discover import discover, feed_info, parse_choice, resolve_feed, same_feed

RSS = b"""<?xml version="1.0"?><rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
<channel><title>Dynasty Nerds</title><itunes:title>ignored</itunes:title>
<item><title>Rookie mocks</title><pubDate>Fri, 03 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Older</title></item>"""  # cut off mid-feed, as a byte-capped read leaves it

PAGE = b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml"></head></html>'


def fake(pages: dict[str, bytes]):
    def get(url):
        if url not in pages:
            raise OSError(f"unreachable: {url}")
        return pages[url], url
    return get


def test_feed_info_reads_a_truncated_feed_and_rejects_html():
    assert feed_info(RSS) == {
        "title": "Dynasty Nerds", "latest_title": "Rookie mocks",
        "latest_published": "Fri, 03 Oct 2026 10:00:00 GMT",
    }
    assert feed_info(PAGE) is None
    assert feed_info(b"not xml") is None


def test_resolve_feed_direct_page_link_and_apple_lookup():
    get = fake({
        "https://f.example/rss": RSS,
        "https://show.example/": PAGE,
        "https://show.example/feed.xml": RSS,
        "https://itunes.apple.com/lookup?id=123": json.dumps({"results": [{"feedUrl": "https://f.example/rss"}]}).encode(),
    })
    assert resolve_feed("https://f.example/rss", get)[0] == "https://f.example/rss"
    assert resolve_feed("https://show.example/", get)[0] == "https://show.example/feed.xml"
    assert resolve_feed("https://podcasts.apple.com/us/podcast/x/id123", get)[0] == "https://f.example/rss"
    with pytest.raises(ValueError):
        resolve_feed("https://show.example/", fake({"https://show.example/": b"<html></html>"}))


def test_discover_dedupes_marks_subscribed_and_keeps_errors_visible():
    results = {"results": [
        {"url": "https://f.example/rss", "title": "A", "engine": "podchaser", "metadata": "12 episodes"},
        {"url": "https://f.example/rss/", "title": "A again", "engine": "fyyd"},
        {"url": "https://other.example/rss", "title": "B", "engine": "podchaser"},
        {"url": "https://down.example/rss", "title": "C", "engine": "podchaser"},
    ]}
    get = fake({
        "https://search/search?q=dynasty&categories=podcasts&format=json": json.dumps(results).encode(),
        "https://f.example/rss": RSS,
        "https://f.example/rss/": RSS,
        "https://other.example/rss": RSS,
    })
    found = discover("https://search", "dynasty", ["http://F.example/rss/"], get=get)
    assert [c.source_url for c in found] == ["https://f.example/rss", "https://other.example/rss", "https://down.example/rss"]
    assert [c.subscribed for c in found] == [True, False, False]
    assert found[0].latest_title == "Rookie mocks" and found[0].about == "12 episodes"
    assert found[2].feed_url is None and "unreachable" in found[2].error


def test_same_feed_and_choice_parsing():
    assert same_feed("https://a.example/feed/", "http://A.example/feed")
    assert not same_feed("https://a.example/feed?x=1", "https://a.example/feed")
    assert parse_choice("1, 3 3", 3) == [0, 2]
    assert parse_choice("", 3) == []
    with pytest.raises(ValueError):
        parse_choice("4", 3)
