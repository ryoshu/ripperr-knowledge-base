from __future__ import annotations

import json

from knowledge_base.news import allowed, find_urls, load_domains


class Store:
    def web_document(self, url):
        return url == "https://espn.com/old" or None


def test_find_urls_filters_domains_dedupes_and_skips_indexed(tmp_path):
    results = [
        {"url": "https://www.espn.com/a?utm_source=x"},
        {"url": "https://espn.com/old"},          # already indexed
        {"url": "https://evil.example/b"},        # not allowlisted
        {"url": "https://www.espn.com/a"},        # same page once tracking is dropped
        {"url": "https://nfl.com/c"},
        {"url": "https://nfl.com/videos/e"},      # video page
        {"url": "https://nfl.com/d"},
    ]
    asked = []

    def get(url):
        asked.append(url)
        return json.dumps({"results": results}).encode(), url

    list_file = tmp_path / "d.txt"
    list_file.write_text("espn.com  # network\nnfl.com\n\n")
    domains = load_domains(list_file)
    assert domains == ["espn.com", "nfl.com"]
    urls = find_urls("https://s", ["Some Player"], domains, Store(), per_player=2, get=get, pause=0)
    assert urls == ["https://www.espn.com/a", "https://nfl.com/c", "https://nfl.com/d"]  # 2nd search: repeats dropped, cap applies to it too
    assert "time_range=week" in asked[0] and "categories=news" in asked[0]
    assert "categories=general" in asked[1] and "site%3Aespn.com+OR+site%3Anfl.com" in asked[1]
    assert not allowed("https://notespn.com/x", ["espn.com"])
