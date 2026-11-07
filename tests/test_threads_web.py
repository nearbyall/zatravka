import json
import random

import pytest

from ideaminer.accounts import add_account, normalize_username, pick_accounts, record_fetch
from ideaminer.config import Direction
from ideaminer.core.models import FeedSpec
from ideaminer.sources.base import SourceUnavailable
from ideaminer.sources.threads import ThreadsApiBackend, ThreadsSource, ThreadsWebBackend, parse_rendered
from ideaminer.sources.threads.web import ProfileUnavailable, is_login_page


def post(pk, user, text, ts=1793900000, likes=5, replies=2, code=None):
    return {"post": {"pk": str(pk), "code": code or f"C{pk}", "taken_at": ts, "like_count": likes,
                     "user": {"username": user}, "caption": {"text": text} if text is not None else None,
                     "text_post_app_info": {"direct_reply_count": replies, "repost_count": 1},
                     "detected_language": "ru"}}


def page(threads, title="Автор (@alice) • Threads, Say more"):
    payload = {"require": [[None, None, {"__bbox": {"result": {"data": {"mediaData": {"edges": [
        {"node": {"thread_items": items}} for items in threads]}}}}}]]}
    return (f"<html><head><title>{title}</title></head><body>"
            f'<script type="application/json" data-sjs>{json.dumps(payload, ensure_ascii=False)}</script>'
            '<script type="application/json">not json</script></body></html>')


LOGIN = "<html><head><title>Threads • Log in</title></head><body>Continue with Instagram</body></html>"


class FakeBrowser:
    available = True

    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def render(self, url):
        self.urls.append(url)
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]


def test_parse_rendered_merges_author_chain():
    html = page([
        [post(1, "alice", "Задолбало вести учёт в экселе."), post(2, "alice", "Есть ли нормальный сервис?"),
         post(3, "bob", "чужой ответ")],
        [post(4, "alice", None)],  # только картинка
        [post(1, "alice", "обрезок")],  # тот же тред в другом блоке, неполный
    ])
    posts = parse_rendered(html, "region_cis", "feed")
    assert len(posts) == 1
    p = posts[0]
    assert p.body == "Задолбало вести учёт в экселе.\n\nЕсть ли нормальный сервис?"
    assert p.url == "https://www.threads.com/@alice/post/C1" and p.author == "alice"
    assert p.score == 5 and p.num_comments == 2 and p.lang == "ru" and p.metrics == {"repost_count": 1}
    assert p.created_at.year == 2026 and p.direction == "region_cis"


def test_login_page_detection():
    assert is_login_page(LOGIN)
    assert not is_login_page(page([[post(1, "alice", "x")]]))


@pytest.fixture
def web_cfg(cfg):
    cfg.sources.threads.web.enabled = True
    cfg.sources.threads.web.min_interval_s = 0
    return cfg


def test_backend_selection(web_cfg, ctx):
    web = ThreadsWebBackend(web_cfg, FakeBrowser([page([[post(1, "alice", "текст")]])]))
    api = ThreadsApiBackend(web_cfg, ctx.http)
    src = ThreadsSource(web_cfg, ctx.http, ctx.db, backends={"api": api, "web": web})
    assert src.status() == (True, "публичные профили без входа (headless-браузер)")
    assert src.kinds() == {"profile"}  # без токена поиска по словам нет
    web_cfg.secrets.threads_access_token = "t"
    assert src.kinds() == {"keyword", "profile"} and src.status()[1] == "официальный Threads API"
    web_cfg.sources.threads.mode = "web"
    assert src.kinds() == {"profile"}
    web_cfg.sources.threads.enabled = False
    assert src.status()[0] is False and src.kinds() == frozenset()


def test_plan_reads_accounts_round_robin(web_cfg, ctx):
    web = ThreadsWebBackend(web_cfg, FakeBrowser([page([[post(1, "alice", "текст")]])]))
    src = ThreadsSource(web_cfg, ctx.http, ctx.db, backends={"web": web, "api": web})
    web_cfg.sources.threads.mode = "web"
    for name in ("@alice", "https://www.threads.com/@bob/post/xyz", "carol"):
        add_account(ctx.db, "threads", name, "region_cis")
    add_account(ctx.db, "threads", "global_guy", None)
    d = Direction(key="region_cis", threads_queries=["есть ли сервис"])
    feeds = src.plan(d, random.Random(1))
    assert [f.kind for f in feeds] == ["profile", "profile"]  # accounts_per_direction=2, поиска нет
    for f in feeds:
        src.fetch(f)
    third = src.plan(d, random.Random(2))
    assert {f.target for f in third} & ({"alice", "bob", "carol"} - {f.target for f in feeds})  # по кругу
    assert [f.target for f in src.plan_global(random.Random())] == ["global_guy"]


def test_circuit_breaker_and_probe(web_cfg, ctx):
    browser = FakeBrowser([LOGIN])
    web = ThreadsWebBackend(web_cfg, browser)
    with pytest.raises(ProfileUnavailable):  # проверка нового имени: страница входа = нет аккаунта
        web.fetch(FeedSpec("threads", "profile", "ghost", "d", {"probe": 1}))
    assert web.status()[0]  # и это не повод выключать режим
    for _ in range(3):
        assert web.fetch(FeedSpec("threads", "profile", "alice", "d")) == []
    ok, why = web.status()
    assert not ok and "отключён" in why
    with pytest.raises(SourceUnavailable):
        web.fetch(FeedSpec("threads", "profile", "alice", "d"))
    assert len(browser.urls) == 4  # после отключения браузер больше не дёргается


def test_accounts_registry(ctx):
    assert normalize_username("@Alice.Biz") == "alice.biz"
    assert normalize_username("https://www.threads.net/@bob_1/post/abc") == "bob_1"
    assert normalize_username("не имя!") is None
    add_account(ctx.db, "threads", "alice", "d")
    for _ in range(3):
        record_fetch(ctx.db, "threads", "alice", 0)
    assert pick_accounts(ctx.db, "threads", "d", 5, random.Random()) == []  # трижды пусто — выключен
    add_account(ctx.db, "threads", "@alice", "d", origin="manual")  # ручное добавление включает обратно
    assert pick_accounts(ctx.db, "threads", "d", 5, random.Random()) == ["alice"]


def test_discover_accounts_validates(web_cfg, ctx):
    import json as _json
    from conftest import FakeProvider
    from ideaminer.planner import sync_directions
    from ideaminer.stages.discover import discover_accounts
    sync_directions(web_cfg, ctx.db)
    pages = {"real_seller": page([[post(1, "real_seller", "Озон опять поменял правила возвратов")]]),
             "old_blog": page([[post(2, "old_blog", "давний пост", ts=1600000000)]])}

    class RoutingBrowser:
        available = True

        def render(self, url):
            return pages.get(url.rsplit("@", 1)[-1], LOGIN)

    ctx.sources["threads"] = ThreadsSource(web_cfg, ctx.http, ctx.db, backends={
        "api": ThreadsApiBackend(web_cfg, ctx.http), "web": ThreadsWebBackend(web_cfg, RoutingBrowser())})
    answer = {"accounts": [{"username": "@real_seller", "why": "селлер маркетплейсов"},
                           {"username": "made_up_name", "why": "выдумка"},
                           {"username": "old_blog", "why": "заброшен"},
                           {"username": "bad name!", "why": "мусор"}]}
    ctx.llm.providers = [FakeProvider(lambda s, u: _json.dumps(answer))]
    assert discover_accounts(ctx, "region_cis", 4) == ["real_seller"]
    row = ctx.db.one("SELECT direction, origin, note FROM accounts WHERE username='real_seller'")
    assert row == {"direction": "region_cis", "origin": "llm", "note": "селлер маркетплейсов"}
    assert ctx.sources["threads"].status()[0]  # выдуманные имена не выключили веб-режим


def relay(name, data):
    """Запись, в которой сервер Threads кладёт в страницу готовый ответ Relay-запроса."""
    return ["RelayPrefetchedStreamCache", "next", [], [f"adp_{name}RelayPreloader_abc", {"__bbox": {"result": {"data": data}}}]]


def chain(*posts):
    return {"node": {"posts": {"edges": [{"node": p["post"]} for p in posts]}}}


def post_page(op="alice", code="P1", replies=(), pinned=(), related=()):
    target = post(100, op, "Где найти бухгалтера для ИП без переплаты?", code=code)["post"]
    entries = [
        relay("BarcelonaPostPageTargetQuery", {"media": target}),
        relay("BarcelonaPostPageDownwardQuery", {"media": {"text_post_app_info": {
            "direct_replies": {"edges": list(replies)}, "pinned_replies": {"edges": list(pinned)}}}}),
        relay("BarcelonaLoggedOutRelatedPostsWWWQuery", {"relatedPosts": {"threads": [
            {"thread_items": [p]} for p in related]}}),
    ]
    payload = {"require": [["ScheduledServerJS", "handle", None, [{"__bbox": {"require": entries}}]]]}
    return (f"<html><head><title>{op} • Threads</title></head><body>"
            f'<script type="application/json" data-sjs>{json.dumps(payload, ensure_ascii=False)}</script></body></html>')


def test_parse_post_page():
    from ideaminer.sources.threads.web import parse_post_page, post_page_url
    html = post_page(
        replies=[chain(post(201, "bob", "Плачу 5 тысяч в месяц, а толку ноль"), post(202, "alice", "Вот-вот")),
                 chain(post(203, "carol", None)), chain(post(204, "dave", "Есть сервис, но дорогой"))],
        pinned=[chain(post(204, "dave", "Есть сервис, но дорогой"))],
        related=[post(301, "eve", "Как я ушла от бухгалтера на аутсорс"), post(302, "frank", None)])
    page = parse_post_page(html, "cis_smb_admin")
    assert page.found
    assert [(c.author, c.body) for c in page.replies] == [
        ("dave", "Есть сервис, но дорогой"),  # закреплённый — первым и без повтора
        ("bob", "Плачу 5 тысяч в месяц, а толку ноль"),
        ("alice (автор)", "Вот-вот"),
    ]
    assert [(p.author, p.direction, p.feed) for p in page.related] == [("eve", "cis_smb_admin", "related")]
    assert not parse_post_page(LOGIN).found
    assert post_page_url("https://www.threads.net/@Bob.1/post/Ab_c-1?x=1") == "https://www.threads.com/@Bob.1/post/Ab_c-1"
    assert post_page_url("https://www.threads.com/@bob") is None


def test_enrich_takes_replies_and_adopts_related_authors(web_cfg, ctx):
    from ideaminer.db import loads
    from ideaminer.core.models import RawPost
    from ideaminer.stages.collect import insert_posts
    from ideaminer.stages.enrich import run_enrich
    web_cfg.sources.threads.mode = "web"
    web_cfg.sources.threads.max_accounts_per_direction = 2
    browser = FakeBrowser([post_page(
        replies=[chain(post(201, "bob", "Плачу 5 тысяч в месяц, а толку ноль"))],
        related=[post(301, "eve", "раз"), post(302, "frank", "два"), post(303, "grace", "три")])])
    ctx.sources["threads"] = ThreadsSource(web_cfg, ctx.http, ctx.db, backends={
        "api": ThreadsApiBackend(web_cfg, ctx.http), "web": ThreadsWebBackend(web_cfg, browser)})
    add_account(ctx.db, "threads", "alice", "cis_smb_admin")
    insert_posts(ctx.db, [RawPost(platform="threads", ext_id="100", url="https://www.threads.net/@alice/post/P1",
                                  author="alice", title=None, body="Где найти бухгалтера?", created_at=None,
                                  direction="cis_smb_admin")], None)
    ctx.db.execute("UPDATE posts SET status='promising', triage_score=8")
    run_enrich(ctx, limit=3)
    assert browser.urls == ["https://www.threads.com/@alice/post/P1"]
    row = ctx.db.one("SELECT comments_json FROM posts")
    assert [c["body"] for c in loads(row["comments_json"])] == ["Плачу 5 тысяч в месяц, а толку ноль"]
    rows = ctx.db.query("SELECT username, direction, origin FROM accounts WHERE origin='related'")
    assert rows == [{"username": "eve", "direction": "cis_smb_admin", "origin": "related"}]  # потолок 2 = alice + eve
