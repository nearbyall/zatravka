from datetime import datetime, timedelta, timezone

from ideaminer.config import Prefilter, Signal
from ideaminer.core.prefilter import detect_lang, evaluate
from ideaminer.core.selection import select_diverse
from ideaminer.core.text import verify_quote
from ideaminer.core.similarity import SimIndex, similarity


def test_detect_lang():
    assert detect_lang("I wish there was an app that would track this for me, it is so annoying") == "en"
    assert detect_lang("Почему до сих пор нет нормального сервиса для учёта, это же так просто") == "ru"
    assert detect_lang("Чому досі немає нормального сервісу, це ж дуже просто і для всіх") == "uk"
    assert detect_lang("Ich habe keine Ahnung, wie das funktioniert und das ist nicht gut") == "de"
    assert detect_lang("") == "unknown"


def test_prefilter_rules():
    cfg = Prefilter(min_chars=20, max_age_days=10, languages=["en", "ru"],
                    spam_patterns=[r"(?i)promo code"],
                    signals=[Signal(pattern=r"(?i)i wish there was", weight=3)])
    now = datetime.now(timezone.utc)
    ok = evaluate(cfg, "Title", "I wish there was an app for this. Is it hard?", now, score=100, num_comments=10)
    assert ok.keep and ok.score > 3 and ok.reason == "signals=1"
    assert evaluate(cfg, None, "short", now).reason == "short"
    assert evaluate(cfg, None, "x" * 30, now - timedelta(days=11)).reason == "old"
    assert evaluate(cfg, None, "use my promo code for the best deal ever ok", now).reason == "spam"
    assert evaluate(cfg, None, "Ich habe keine Ahnung, wie das funktioniert und das ist nicht", now).reason == "lang:de"
    assert evaluate(cfg, None, "a perfectly normal post body text here", now, nsfw=True).reason == "nsfw"
    plain = evaluate(cfg, None, "a perfectly normal post body text here", now)
    assert plain.keep and plain.score < ok.score


def test_similarity_morphology():
    a = "Сервис напоминаний о поливе комнатных растений"
    b = "Напоминалка для полива комнатного растения"
    c = "Маркетплейс подержанных запчастей для велосипедов"
    assert similarity(a, b) > similarity(a, c) + 0.2
    idx = SimIndex([a, b, c])
    idx.add(1, b)
    idx.add(2, c)
    assert idx.query(a)[0][0] == 1
    idx.remove(1)
    assert all(doc != 1 for doc, _ in idx.query(a))


def test_verify_quote():
    src = "Title\nI have 40 plants. I wish there was an app that tracks my plant watering by species!"
    assert verify_quote("i wish there was an app that tracks my plant watering", src)
    assert verify_quote("I have 40 plants, I wish there was an app that tracks my plant watering by kind", src)
    assert not verify_quote("completely invented sentence that the model hallucinated here", src)
    assert not verify_quote("", src)


def test_select_diverse():
    rows = [{"id": i, "direction": "shop"} for i in range(8)] + [{"id": 100 + i, "direction": d} for i, d in enumerate("ab")]
    got = select_diverse(rows, 5, 0.4)
    assert [r["id"] for r in got] == [0, 1, 100, 101, 2]  # 2 из «shop», затем другие, остаток — снова лучшие
    assert len(select_diverse(rows[:3], 5, 0.4)) == 3
    assert [r["id"] for r in select_diverse(rows, 3, 1.0)] == [0, 1, 2]


def test_dotenv_last_nonempty_wins(tmp_path, monkeypatch):
    from ideaminer.config import load_dotenv
    for k in ("T_EMPTY_THEN_SET", "T_QUOTED", "T_FROM_ENV"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("T_FROM_ENV", "outer")
    env = tmp_path / ".env"
    env.write_text("T_EMPTY_THEN_SET=\n# c\nT_QUOTED='a b'\nT_FROM_ENV=file\nT_EMPTY_THEN_SET=tok123\n")
    load_dotenv(env)
    import os
    assert os.environ["T_EMPTY_THEN_SET"] == "tok123"
    assert os.environ["T_QUOTED"] == "a b"
    assert os.environ["T_FROM_ENV"] == "outer"


def test_explore_overlap():
    from ideaminer.config import Direction
    from ideaminer.pipeline.stages.explore import overlapping
    dirs = {"ecom": Direction(key="ecom", subreddits=["shopify", "AmazonSeller", "ecommerce", "Etsy"])}
    assert overlapping(["Shopify", "amazonseller", "FulfillmentByAmazon"], dirs, {}) == "ecom"
    assert overlapping(["Beekeeping", "shopify", "gardening"], dirs, {}) is None
    assert overlapping(["a", "b"], dirs, {"new1": ["a", "b", "c"]}) == "new1"


def test_safe_url_keeps_only_web_links():
    from ideaminer.web.views import safe_url
    assert safe_url("https://www.threads.com/@a/post/B") == "https://www.threads.com/@a/post/B"
    assert safe_url("JaVaScRiPt:alert(1)") == "#"
    assert safe_url("data:text/html,x") == "#"
    assert safe_url(None) == "#"
