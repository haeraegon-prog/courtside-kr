import importlib.util
from datetime import datetime, timezone
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('build_news', Path(__file__).parents[1] / 'scripts/build_news.py')
import sys
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def test_cutoff_korea_before_and_after_nine():
    before = datetime(2026, 10, 11, 23, 59, tzinfo=timezone.utc)  # KST next day 08:59
    after = datetime(2026, 10, 12, 0, 1, tzinfo=timezone.utc)    # KST next day 09:01
    start_a, end_a = mod.cutoff_for(before)
    start_b, end_b = mod.cutoff_for(after)
    assert end_a.isoformat() == '2026-10-11T09:00:00+09:00'
    assert end_b.isoformat() == '2026-10-12T09:00:00+09:00'
    assert (end_b-start_b).total_seconds() == 86400


def test_rss_google_source_and_pubdate():
    xml = b'''<?xml version="1.0"?><rss version="2.0"><channel><item>
    <title>Dallas Mavericks sign guard</title><link>https://example.com/a</link>
    <description><![CDATA[<p>Sample game report</p>]]></description>
    <pubDate>Sun, 11 Oct 2026 22:30:00 GMT</pubDate><source>ESPN</source>
    </item></channel></rss>'''
    result=mod.parse_rss(xml)
    assert len(result)==1
    assert result[0]['source']=='ESPN'
    assert mod.to_utc(result[0]).isoformat()=='2026-10-11T22:30:00+00:00'
    assert mod.clean_html(result[0]['summary'])=='Sample game report'


def test_rss_timestamp_without_timezone_is_rejected():
    assert mod.to_utc({'published': '2026-10-12T08:30:00'}) is None
    assert mod.to_utc({'published': 'Mon, 12 Oct 2026 08:30:00'}) is None


def test_article_date_uses_korea_calendar_day():
    assert mod.published_kst_date('2026-10-11T16:30:00+00:00').isoformat() == '2026-10-12'
    assert mod.published_kst_date('2026-10-11').isoformat() == '2026-10-11'


def test_team_classification_and_player_detection():
    assert mod.find_teams('Boston Celtics play the Lakers') == ['BOS', 'LAL']
    assert mod.find_teams('heat wave in July') == []
    assert mod.find_teams('The Heat sign a player') == ['MIA']
    assert mod.is_lee('Hyun-Jung Lee signs with team')
    assert mod.is_lee('이현중 선수의 NBA 도전')
    assert not mod.is_lee('Lee scores 12 points')


def test_official_domain_matching_does_not_accept_lookalike_domains():
    assert mod.is_domain_or_subdomain('www.nba.com', 'nba.com')
    assert not mod.is_domain_or_subdomain('nba.com.fake.test', 'nba.com')
    assert not mod.is_domain_or_subdomain('fakenba.com', 'nba.com')


def test_category_and_title_hash():
    assert mod.categorize('Grizzlies center out two weeks with ankle injury')=='부상'
    assert mod.categorize('player signs two-way contract')=='계약·이적'
    assert mod.article_id('https://abc.com/news/1', 'Lakers win') == mod.article_id('https://abc.com/news/1','Lakers win')


def test_failed_feeds_keep_older_data(tmp_path, monkeypatch):
    import json
    out = tmp_path / "news.json"
    old = {"mode": "automatic", "generated_at": "2026-10-10T00:00:00Z", "articles": [{"id": "abc"}]}
    out.write_text(json.dumps(old))
    monkeypatch.setattr(mod, 'OUTPUT', out)
    monkeypatch.setattr(mod, 'feeds', lambda: [mod.Feed('https://invalid.test/rss', 'nba', 'Fail Source', 1)])
    def broken(*args):
        raise ConnectionError('network down')
    monkeypatch.setattr(mod, 'fetch_feed', broken)
    result = mod.build(datetime(2026, 10, 12, tzinfo=timezone.utc))
    assert result['articles'] == old['articles']
    assert result['generated_at'] == old['generated_at']
    assert result['warnings']


def test_only_09_to_09_window_selected(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'OUTPUT', tmp_path / 'missing.json')
    monkeypatch.setattr(mod, 'feeds', lambda: [mod.Feed('https://valid.test/rss', 'nba', 'Valid Source', 1)])
    def fake_feed(*args):
        return [
            {'id': 'in', 'section': 'nba', 'title_original': 'Lakers big win', 'url': 'https://espn.com/a', 'source': 'ESPN', 'published_at': '2026-10-11T01:00:00+00:00', 'priority': 1, 'category': '경기·기록', 'teams': ['LAL']},
            {'id': 'out', 'section': 'nba', 'title_original': 'Celtics old win', 'url': 'https://espn.com/b', 'source': 'ESPN', 'published_at': '2026-10-10T23:59:00+00:00', 'priority': 1, 'category': '경기·기록', 'teams': ['BOS']},
        ]
    monkeypatch.setattr(mod, 'fetch_feed', fake_feed)
    monkeypatch.setattr(mod, 'translate', lambda article, session, client: article)
    result=mod.build(datetime(2026,10,12,0,15,tzinfo=timezone.utc))
    assert {a['id'] for a in result['articles']} == {'in'}
    assert result['cutoff_at'] == '2026-10-12T09:00:00+09:00'


def test_duplicate_headlines_across_feeds_are_kept_once(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'OUTPUT', tmp_path / 'missing.json')
    monkeypatch.setattr(mod, 'feeds', lambda: [
        mod.Feed('https://a.test/rss', 'nba', 'ESPN', 1),
        mod.Feed('https://b.test/rss', 'nba', 'Google News', 2),
    ])
    duplicate = {
        'id': 'first', 'section': 'nba', 'title_original': 'Lakers win season opener',
        'url': 'https://espn.com/a', 'source': 'ESPN',
        'published_at': '2026-10-11T01:00:00+00:00', 'priority': 1,
        'category': '경기·기록', 'teams': ['LAL'],
    }
    syndicated = {**duplicate, 'id': 'second', 'title_original': 'Lakers win season opener - ESPN',
                  'url': 'https://news.google.com/rss/articles/abc', 'priority': 2}
    monkeypatch.setattr(mod, 'fetch_feed', lambda feed, session: [duplicate if feed.source == 'ESPN' else syndicated])
    monkeypatch.setattr(mod, 'translate', lambda article, session, client: article)
    monkeypatch.setattr(mod.time, 'sleep', lambda _: None)
    result = mod.build(datetime(2026, 10, 12, 0, 15, tzinfo=timezone.utc))
    assert [a['id'] for a in result['articles']] == ['first']


def test_partial_feed_failure_keeps_successful_articles(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'OUTPUT', tmp_path / 'missing.json')
    good = mod.Feed('https://good.test/rss', 'nba', 'Good Feed', 1)
    bad = mod.Feed('https://bad.test/rss', 'lee', 'Lee Feed', 0)
    article = {
        'id': 'kept', 'section': 'nba', 'title_original': 'Lakers win opener',
        'url': 'https://nba.com/news/1', 'source': 'NBA.com',
        'published_at': '2026-10-11T01:00:00+00:00', 'priority': 1,
        'category': '경기·기록', 'teams': ['LAL'],
    }
    monkeypatch.setattr(mod, 'feeds', lambda: [good, bad])
    monkeypatch.setattr(mod, 'fetch_feed', lambda feed, session: [article] if feed == good else (_ for _ in ()).throw(ConnectionError('offline')))
    monkeypatch.setattr(mod, 'translate', lambda item, session, client: item)
    monkeypatch.setattr(mod.time, 'sleep', lambda _: None)
    result = mod.build(datetime(2026, 10, 12, 0, 15, tzinfo=timezone.utc))
    assert [item['id'] for item in result['articles']] == ['kept']
    assert any('Lee Feed(lee)' in warning for warning in result['warnings'])
