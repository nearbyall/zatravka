import copy
import json
import random

import pytest
from conftest import FIXTURES
from fastapi.testclient import TestClient

from zatravka.storage.db import loads
from zatravka.storage.idea_queries import query_ideas
from zatravka.storage.ideas import rescore_all
from zatravka.storage.runs import acquire_lock, release_lock
import io

from openpyxl import load_workbook

from zatravka.export import export_rows, to_csv, to_json, to_markdown, to_xlsx
from zatravka.core.taxonomy import resolve_taxonomy
from zatravka.pipeline import run_once
from zatravka.pipeline.planner import make_plan, pick_directions
from zatravka.sources.reddit import RedditSource, parse_rss_comments, parse_rss_posts
from zatravka.pipeline.stages.digest import run_digest
from zatravka.web.app import create_app
from zatravka.web.views import plural

LISTING = (FIXTURES / "reddit_listing.atom").read_bytes()
COMMENTS = (FIXTURES / "reddit_comments.atom").read_bytes()


@pytest.fixture
def fake_reddit(monkeypatch):
    def fetch(self, feed):
        posts = parse_rss_posts(LISTING, feed.direction, feed.key)
        if feed.kind == "search":
            p = copy.copy(posts[0])
            p.ext_id = "t3_second"
            p.body = posts[0].body + " Also I wish it had reminders for fertilizer."
            return [p]
        return posts

    monkeypatch.setattr(RedditSource, "fetch", fetch)
    monkeypatch.setattr(RedditSource, "comments", lambda self, post: parse_rss_comments(COMMENTS, 5))
    monkeypatch.setattr(RedditSource, "subreddit_exists", lambda self, sub: sub != "apiculture")


def test_full_pipeline(ctx, fake_reddit):
    res = run_once(ctx, only_directions=["pain_hunting"], explore=True)
    assert res["status"] == "ok", res
    db = ctx.db

    statuses = {r["ext_id"]: r["status"] for r in db.query("SELECT ext_id, status FROM posts")}
    assert statuses == {"t3_1abcde": "extracted", "t3_second": "extracted", "t3_1zzzzz": "filtered"}
    enriched = db.query("SELECT comments_json FROM posts WHERE comments_json IS NOT NULL")
    assert enriched and "spreadsheet" in enriched[0]["comments_json"]

    ideas = db.query("SELECT * FROM ideas ORDER BY id")
    assert len(ideas) == 4
    clusters = [i for i in ideas if i["cluster_id"] == i["id"]]
    assert len(clusters) == 2 and all(c["mentions"] == 2 for c in clusters)
    remind = next(i for i in clusters if i["title"].startswith("Напоминалка"))
    assert remind["market"] == "B2C" and remind["sphere"] == "home" and remind["nature"] == "digital"
    assert loads(remind["spheres_json"]) == ["hobbies"]
    # the "СНГ" label maps to the cis key, and "global" next to concrete regions is dropped
    assert loads(remind["regions_json"]) == ["cis", "eu"] and remind["origin_region"] == "north_america"
    assert remind["base_score"] == 6.15 and remind["score"] == 6.55  # plus the bonus for 2 mentions
    verified = {r["quote_verified"] for r in db.query(
        "SELECT e.quote_verified FROM idea_evidence e JOIN ideas i ON i.id=e.idea_id WHERE i.insight_type='explicit'")}
    assert verified == {1}
    assert {r["quote_verified"] for r in db.query(
        "SELECT e.quote_verified FROM idea_evidence e JOIN ideas i ON i.id=e.idea_id WHERE i.insight_type='lateral'")} == {0}

    new_dir = db.one("SELECT * FROM directions WHERE key='beekeeping_pros'")
    assert new_dir and new_dir["origin"] == "llm"
    assert loads(new_dir["spec_json"])["subreddits"] == ["Beekeeping"]

    run = db.one("SELECT * FROM runs WHERE id=?", (res["run_id"],))
    stats = loads(run["stats_json"])
    assert run["status"] == "ok" and stats["extract"]["ideas"] == 4 and run["cost_usd"] > 0
    assert db.kv_get("run_lock") is None

    # Reprocessing without collection breaks nothing and creates no duplicates
    again = run_once(ctx, collect=False)
    assert again["status"] == "ok" and db.scalar("SELECT COUNT(*) FROM ideas") == 4

    assert query_ideas(db, region="cis")[1] == 2  # its own plus the global one
    assert query_ideas(db, region="cis", with_global=False)[1] == 1
    assert query_ideas(db, region="latam", with_global=False)[1] == 0
    assert query_ideas(db, origin="north_america")[1] == 2

    rows, total = query_ideas(db, market="B2B")
    assert total == 1 and rows[0]["title"].startswith("Датчики")
    items = export_rows(db, rows)
    assert items[0]["sources"][0]["url"].startswith("https://www.reddit.com/")
    assert "Датчики" in to_csv(items) and json.loads(to_json(items))[0]["market"] == "B2B"
    assert "## Датчики" in to_markdown(items)

    all_rows, _ = query_ideas(db)
    all_items = export_rows(db, all_rows)
    all_items[0]["note"] = "=HYPERLINK(\"http://evil\")"  # post text must not become a formula
    wb = load_workbook(io.BytesIO(to_xlsx(all_items, resolve_taxonomy(ctx.cfg))))
    assert wb.sheetnames == ["Идеи", "Источники", "Сфера × рынок", "Сфера × регион"]
    ideas_ws = wb["Идеи"]
    header = [c.value for c in ideas_ws[1]]
    assert header[:4] == ["ID", "Балл", "Название", "Суть"] and "Спрос" in header
    assert ideas_ws.freeze_panes == "D2" and ideas_ws.auto_filter.ref.startswith("A1:")
    first = {h: c for h, c in zip(header, ideas_ws[2])}
    assert first["Сфера"].value == "Дом, ремонт, интерьер" and first["Тип продукта"].value == "цифровой"
    assert first["Заметка"].data_type == "s"
    assert first["Источник"].hyperlink.target.startswith("https://www.reddit.com/")
    assert wb["Источники"].max_row == 1 + sum(len(i["sources"]) for i in all_items)
    pivot = {r[0].value: r[-1].value for r in wb["Сфера × рынок"].iter_rows(min_row=2)}
    assert pivot == {"Дом, ремонт, интерьер": 2}
    assert "'=HYPERLINK" in to_csv(all_items)
    region_sheet = [[c.value for c in r] for r in wb["Сфера × регион"].iter_rows()]
    assert region_sheet[0] == ["Сфера", "глобально", "ЕС", "СНГ", "Идей"]
    assert region_sheet[1] == ["Дом, ремонт, интерьер", 1, 1, 1, 2]
    assert first["Регионы запуска"].value in ("СНГ, ЕС", "глобально")

    path = run_digest(ctx, days=1)
    text = path.read_text(encoding="utf-8")
    assert "Напоминалка полива" in text and "Дом, ремонт, интерьер" in text

    ctx.cfg.scoring.weights = {"demand": 1.0}
    rescore_all(ctx.db, ctx.cfg.scoring.weights, ctx.cfg.scoring.mention_bonus)
    assert db.one("SELECT base_score FROM ideas WHERE id=?", (remind["id"],))["base_score"] == 7.0


def test_budget_stops_run_gracefully(ctx, fake_reddit):
    ctx.cfg.budget.per_run_usd = 0.0003  # enough for exactly one call (≈ $0.0005)
    res = run_once(ctx, only_directions=["pain_hunting"])
    assert res["status"] == "budget"
    assert ctx.db.scalar("SELECT COUNT(*) FROM posts WHERE status IN ('promising','candidate')") > 0


def test_run_lock(ctx):
    assert acquire_lock(ctx.db, "a")
    assert not acquire_lock(ctx.db, "b")
    assert run_once(ctx, collect=False)["status"] == "busy"
    release_lock(ctx.db)
    assert acquire_lock(ctx.db, "c")


def test_planner(ctx):
    ctx.cfg.secrets.x_bearer_token = "t"
    ctx.cfg.secrets.threads_access_token = "t"
    plan = make_plan(ctx)
    assert plan.directions[0] == "pain_hunting" and "wildcard" in plan.directions
    assert len(plan.directions) == 1 + ctx.cfg.planner.random_directions + 1
    sources = {f.source for f in plan.feeds}
    assert {"reddit", "x", "threads"} <= sources
    assert len({f.key for f in plan.feeds}) == len(plan.feeds)


def test_pick_directions_prefers_productive(ctx):
    from zatravka.config import Direction
    dirs = {k: Direction(key=k) for k in ("good", "bad", "new")}
    stats = {"good": {"yield": 0.5}, "bad": {"yield": 0.01}}
    rng = random.Random(0)
    picks = [pick_directions(dirs, stats, 1, 0.1, rng, set())[0] for _ in range(400)]
    assert picks.count("good") > picks.count("new") > picks.count("bad")


def test_web(ctx, fake_reddit):
    run_once(ctx, only_directions=["pain_hunting"])
    client = TestClient(create_app(ctx.cfg))
    for url in ("/", "/?market=B2C&sphere=home", "/?q=полив&sort=new", "/?axis=region", "/?axis=nature",
                "/?region=cis&glob=0", "/?origin=north_america", "/posts", "/posts?status=extracted",
                "/directions", "/runs", "/healthz", "/api/ideas", "/api/run"):
        assert client.get(url).status_code == 200, url
    region_map = client.get("/?axis=region").text
    assert "Шахматка: сфера и регион" in region_map and ">СНГ<" in region_map
    only_cis = client.get("/?region=cis&glob=0").text
    assert "Напоминалка" in only_cis and "Датчики" not in only_cis
    idea_id = ctx.db.scalar("SELECT id FROM ideas WHERE cluster_id=id ORDER BY id LIMIT 1")
    page = client.get(f"/idea/{idea_id}")
    assert page.status_code == 200 and "Откуда идея" in page.text
    assert client.get("/idea/99999").status_code == 404
    assert client.post(f"/api/ideas/{idea_id}/status", json={"status": "starred"}).json()["ok"]
    assert client.post(f"/api/ideas/{idea_id}/status", json={"status": "nope"}).status_code == 400
    assert client.post(f"/api/ideas/{idea_id}/note", json={"note": "проверить"}).json()["ok"]
    assert ctx.db.one("SELECT status, note FROM ideas WHERE id=?", (idea_id,)) == {"status": "starred", "note": "проверить"}
    assert client.post("/api/directions/saas_founders/toggle").json()["enabled"] is False
    assert client.post("/api/directions/wildcard/toggle").status_code == 400
    csv = client.get("/export.csv?market=B2C")
    assert csv.status_code == 200 and "Напоминалка" in csv.text
    xlsx = client.get("/export.xlsx?market=B2C")
    assert xlsx.status_code == 200 and xlsx.headers["content-type"].startswith("application/vnd.openxml")
    assert load_workbook(io.BytesIO(xlsx.content))["Идеи"].max_row == 2


def test_plural():
    assert plural(1, "идея", "идеи", "идей") == "1 идея"
    assert plural(3, "идея", "идеи", "идей") == "3 идеи"
    assert plural(11, "идея", "идеи", "идей") == "11 идей"
    assert plural(22, "идея", "идеи", "идей") == "22 идеи"


def test_region_focus_and_hints(ctx, fake_reddit):
    ctx.cfg.planner.regions = ["cis"]
    plan = make_plan(ctx)
    assert "region_cis" in plan.directions
    assert not any(d.startswith("region_") and d != "region_cis" for d in plan.directions)
    run_once(ctx, only_directions=["region_cis"])
    hints = {r["region_hint"] for r in ctx.db.query("SELECT region_hint FROM posts")}
    assert hints == {"cis"}  # comes from the direction although the posts are in English
    ideas = ctx.db.query("SELECT origin_region FROM ideas WHERE origin_region IS NOT NULL")
    assert ideas and {i["origin_region"] for i in ideas} == {"cis"}  # the hint reached the classifier


def test_threads_token_refresh(ctx, monkeypatch):
    import zatravka.sources.threads.auth as th
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "env-token")
    ctx.cfg.secrets.threads_access_token = "env-token"
    calls = []
    monkeypatch.setattr(th, "refresh_token", lambda t: calls.append(t) or {"access_token": f"fresh-{len(calls)}"})
    assert th.ensure_token(ctx.cfg, ctx.db) == "fresh-1"
    assert th.ensure_token(ctx.cfg, ctx.db) == "fresh-1" and len(calls) == 1  # no refresh within three days
    monkeypatch.setenv("THREADS_ACCESS_TOKEN", "new-env-token")  # a new token was put into .env
    assert th.ensure_token(ctx.cfg, ctx.db) == "fresh-2" and calls[-1] == "new-env-token"


def test_http_error_hides_tokens():
    from zatravka.net.http import HttpError
    e = HttpError(400, "https://graph.threads.net/v1.0/me?access_token=SECRET&fields=id", "")
    assert "SECRET" not in str(e) and "access_token=***" in str(e)


def test_boost_regions_raises_odds(ctx):
    counts = {"boost": 0, "plain": 0}
    for seed in range(400):
        ctx.rng.seed(seed)
        ctx.cfg.planner.boost_regions = {"cis": 3} if seed % 2 else {}
        cis = sum(1 for k in make_plan(ctx).directions if k.startswith(("region_cis", "cis_")))
        counts["boost" if seed % 2 else "plain"] += cis
    # 7 CIS directions of 46: ≈ 0.6 per run without a boost, ≈ 1.2 with ×3 (the epsilon share is pure chance)
    assert counts["boost"] > counts["plain"] * 1.4


def test_scoped_run_processes_only_its_posts(ctx, fake_reddit):
    from zatravka.core.models import RawPost
    from zatravka.storage.posts import insert_posts
    # The queue holds a Reddit backlog with strong signals; a run limited to X must leave it alone
    backlog = [RawPost(platform="reddit", ext_id=f"t3_old{i}", url=f"https://reddit.com/{i}", author="a",
                       title="Is there an app for this? I would pay", body="I hate doing this manually " * 5,
                       created_at=None, direction="pain_hunting", feed="reddit:listing:smallbusiness")
               for i in range(5)]
    insert_posts(ctx.db, backlog, None)
    ctx.db.execute("UPDATE posts SET status='candidate', prefilter_score=9")
    calls = []
    ctx.sources["x"].fetch = lambda feed: calls.append(feed) or [RawPost(
        platform="x", ext_id="x1", url="https://x.com/u/status/1", author="u", title=None,
        body="Есть ли сервис, чтобы не вести учёт заказов в тетради? Бесит всё делать вручную каждый вечер,"
             " а нормального приложения для маленькой мастерской так и не нашла.",
        created_at=None, lang="ru", direction=feed.direction, feed=feed.key)]
    ctx.sources["x"].status = lambda: (True, "тест")
    explored = []
    import zatravka.pipeline.runner as pl
    orig = pl.run_explore
    pl.run_explore = lambda c: explored.append(1)
    try:
        res = run_once(ctx, only_sources=["x"], only_directions=["region_cis"])
    finally:
        pl.run_explore = orig
    assert res["status"] == "ok" and calls
    by_platform = {r["platform"]: r["status"] for r in ctx.db.query("SELECT platform, status FROM posts")}
    assert by_platform["reddit"] == "candidate"  # the backlog is untouched
    assert by_platform["x"] in ("extracted", "skipped", "promising")
    assert not explored and not ctx.scope  # a limited run does not explore, and the scope is reset


def test_triage_threshold_per_platform(ctx):
    import json as _json
    import re as _re
    from conftest import FakeProvider
    from zatravka.core.models import RawPost
    from zatravka.pipeline.stages.triage import run_triage
    from zatravka.storage.posts import insert_posts
    ctx.cfg.triage.threshold, ctx.cfg.triage.platform_thresholds = 7, {"threads": 5}
    insert_posts(ctx.db, [RawPost(platform=p, ext_id=p, url=f"https://{p}.example/1", author="a", title=None,
                                  body=f"Пост с {p}: бесит считать всё вручную", created_at=None)
                          for p in ("threads", "reddit")], None)
    ctx.db.execute("UPDATE posts SET status='candidate', prefilter_score=1")
    ctx.llm.providers = [FakeProvider(lambda s, u: _json.dumps({"items": [
        {"id": int(i), "score": 5, "signals": [], "note": ""} for i in _re.findall(r"### id=(\d+)", u)]}))]
    run_triage(ctx)
    statuses = {r["platform"]: r["status"] for r in ctx.db.query("SELECT platform, status FROM posts")}
    assert statuses == {"threads": "promising", "reddit": "skipped"}
