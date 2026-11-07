import json
import random

from conftest import FIXTURES

from ideaminer.config import Direction
from ideaminer.core.models import FeedSpec
from ideaminer.sources.inbox import InboxSource
from ideaminer.sources.reddit import RedditSource, parse_api_comments, parse_api_listing, parse_rss_comments, parse_rss_posts
from ideaminer.sources.rss import parse_feed
from ideaminer.sources.threads import parse_threads
from ideaminer.sources.x import parse_search


def test_reddit_rss_listing():
    posts = parse_rss_posts((FIXTURES / "reddit_listing.atom").read_bytes(), "d", "feed")
    assert [p.ext_id for p in posts] == ["t3_1abcde", "t3_1zzzzz"]
    p = posts[0]
    assert p.author == "plantlady" and p.community == "houseplants"
    assert p.title == "Help: I keep overwatering my plants"
    assert "I wish there was an app that tracks my plant watering" in p.body
    assert "• Tried 3 apps" in p.body
    assert "submitted by" not in p.body and "[comments]" not in p.body
    assert "& syncs" in p.body
    assert p.created_at.isoformat() == "2026-11-05T09:00:00+00:00"
    assert posts[1].body == ""  # пост-картинка без текста


def test_reddit_rss_comments_skip_post_and_deleted():
    comments = parse_rss_comments((FIXTURES / "reddit_comments.atom").read_bytes(), 10)
    assert len(comments) == 1
    assert comments[0].author == "greenthumb"
    assert "It's a nightmare" in comments[0].body


def test_reddit_api_listing_and_comments():
    listing = {"data": {"children": [
        {"kind": "t3", "data": {"name": "t3_a", "permalink": "/r/x/comments/a/t/", "author": "u", "title": "T",
                                "selftext": "body", "created_utc": 1793900000, "subreddit": "x", "score": 12,
                                "num_comments": 3, "over_18": False, "upvote_ratio": 0.9, "is_self": True}},
        {"kind": "t3", "data": {"name": "t3_b", "stickied": True}},
    ]}}
    posts = parse_api_listing(listing)
    assert len(posts) == 1 and posts[0].score == 12 and posts[0].url == "https://www.reddit.com/r/x/comments/a/t/"
    comments = parse_api_comments([{}, {"data": {"children": [
        {"kind": "t1", "data": {"body": "low", "score": 1, "author": "a"}},
        {"kind": "t1", "data": {"body": "high", "score": 50, "author": "b"}},
        {"kind": "t1", "data": {"body": "[removed]", "score": 99}},
        {"kind": "more", "data": {}},
    ]}}], 5)
    assert [c.body for c in comments] == ["high", "low"]


def test_reddit_plan_and_urls(cfg, ctx):
    rc = cfg.sources.reddit
    rc.subs_per_feed, rc.feeds_per_direction, rc.searches_per_direction, rc.items_per_feed = 4, 2, 1, 50
    src = RedditSource(cfg, ctx.http)
    d = Direction(key="k", subreddits=["a", "b", "c", "d", "e"], reddit_search=['"pain"'])
    feeds = src.plan(d, random.Random(1))
    kinds = [f.kind for f in feeds]
    assert kinds.count("listing") == 2 and kinds.count("search") == 1
    search = next(f for f in feeds if f.kind == "search")
    url = src.feed_url(search)
    assert url.startswith("https://www.reddit.com/r/a+b+c+d+e/search.rss?") and "restrict_sr=on" in url
    site = src.feed_url(FeedSpec("reddit", "search", "x y", "k", {"subs": "", "t": "week"}))
    assert site.startswith("https://www.reddit.com/search.rss?q=x+y")
    listing = src.feed_url(FeedSpec("reddit", "listing", "a+b", "k", {"listing": "top_week"}))
    assert listing == "https://www.reddit.com/r/a+b/top/.rss?limit=50&t=week"


def test_x_parse():
    data = {"data": [{"id": "1", "text": "short", "author_id": "u1", "created_at": "2026-11-01T10:00:00.000Z",
                      "lang": "en", "public_metrics": {"like_count": 5, "retweet_count": 2, "reply_count": 1},
                      "note_tweet": {"text": "long full text"}}],
            "includes": {"users": [{"id": "u1", "username": "alice"}]}}
    p = parse_search(data)[0]
    assert p.url == "https://x.com/alice/status/1" and p.body == "long full text"
    assert p.score == 9 and p.num_comments == 1 and p.lang == "en"


def test_threads_parse():
    data = {"data": [{"id": "9", "text": "хоть бы кто-нибудь сделал нормальный трекер", "username": "bob",
                      "permalink": "https://www.threads.com/@bob/post/x", "timestamp": "2026-11-01T10:00:00+0000",
                      "media_type": "TEXT_POST"}, {"id": "10", "media_type": "IMAGE"}]}
    posts = parse_threads(data)
    assert len(posts) == 1 and posts[0].author == "bob"
    assert posts[0].created_at.isoformat() == "2026-11-01T10:00:00+00:00"


def test_rss2_parse():
    xml = """<?xml version="1.0"?><rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
    <item><title>Hello</title><link>https://ex.com/1</link><guid>g1</guid><dc:creator>me</dc:creator>
    <description>&lt;p&gt;Body text&lt;/p&gt;</description><pubDate>Wed, 05 Nov 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    items = parse_feed(xml)
    assert items[0]["id"] == "g1" and items[0]["author"] == "me" and "Body text" in items[0]["html"]


def test_inbox_formats(cfg, ctx, tmp_path):
    inbox = cfg.path(cfg.storage.inbox_dir)
    inbox.mkdir(parents=True)
    (inbox / "x_export.json").write_text(json.dumps({"items": [
        {"id": 5, "full_text": "I would pay for this tool", "user": {"username": "zed"}, "likes": "7"}]}))
    (inbox / "threads_dump.csv").write_text("id,text,username,date\n1,есть ли сервис для учета,ann,2026-11-01\n")
    (inbox / "misc.jsonl").write_text('{"title":"T","body":"B","url":"https://e.com/1"}\n\n')
    src = InboxSource(cfg, ctx.http)
    feeds = src.plan_global(random.Random())
    got = {f.target.split("/")[-1]: src.fetch(f) for f in feeds}
    assert got["x_export.json"][0].platform == "x" and got["x_export.json"][0].author == "zed"
    assert got["x_export.json"][0].score == 7
    assert got["threads_dump.csv"][0].platform == "threads"
    assert got["misc.jsonl"][0].platform == "import" and got["misc.jsonl"][0].ext_id == "https://e.com/1"
    for f in feeds:
        src.after_ingest(f)
    assert not src.files() and len(list((inbox / "processed").iterdir())) == 3


def test_x_daily_read_limit(cfg, ctx, monkeypatch):
    import pytest
    from ideaminer.sources.base import SourceLimitReached
    from ideaminer.sources.x import XSource

    class Resp:
        def json(self):
            return {"data": [{"id": str(i), "text": "t", "author_id": "u"} for i in range(10)],
                    "includes": {"users": [{"id": "u", "username": "a"}]}}

    calls = []
    monkeypatch.setattr(ctx.http, "get", lambda url, **kw: calls.append(kw["params"]) or Resp())
    cfg.secrets.x_bearer_token = "t"
    cfg.sources.x.max_results, cfg.sources.x.daily_read_limit = 10, 25
    src = XSource(cfg, ctx.http, ctx.db)
    feed = FeedSpec("x", "search", '"a" OR "b"', "d")
    assert len(src.fetch(feed)) == 10 and len(src.fetch(feed)) == 10
    assert src.reads_today() == 20
    with pytest.raises(SourceLimitReached):
        src.fetch(feed)  # 20 + 10 > 25 — запрос даже не уходит
    assert len(calls) == 2 and calls[0]["query"] == '("a" OR "b") -is:retweet -is:reply'
    assert "start_time" not in calls[0] and calls[1]["start_time"].endswith("Z")  # второй раз — только новее
