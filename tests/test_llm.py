import json

import pytest
from conftest import FakeProvider

from ideaminer.llm import BudgetExceeded, LLMError, ProviderDown, extract_json
from ideaminer.llm.schemas import TriageOut


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('Вот ответ: {"a": [1, 2]} — готово') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        extract_json("нет json")


def test_retry_on_bad_json_then_ok(ctx):
    answers = iter(["не json", json.dumps({"items": [{"id": 1, "score": 7}]})])
    fake = FakeProvider(lambda s, u: next(answers))
    ctx.llm.providers = [fake]
    out = ctx.llm.json("triage", "sys", "user", TriageOut)
    assert out.items[0].score == 7
    assert len(fake.calls) == 2 and "не прошёл проверку" in fake.calls[1][1]
    rows = ctx.db.query("SELECT ok, cost_usd FROM llm_calls ORDER BY id")
    assert [r["ok"] for r in rows] == [0, 1] and all(r["cost_usd"] > 0 for r in rows)


def test_fallback_to_next_provider(ctx):
    class Down(FakeProvider):
        name = "down"

        def complete(self, *a, **k):
            raise ProviderDown("402 нет денег")

    backup = FakeProvider(lambda s, u: '{"items": []}')
    ctx.llm.providers = [Down(lambda s, u: ""), backup]
    assert ctx.llm.json("triage", "s", "u", TriageOut).items == []
    assert "down" in ctx.llm._down
    assert ctx.llm.json("triage", "s", "u", TriageOut).items == []  # a provider that is down is not called again
    assert len(backup.calls) == 2


def test_all_providers_fail(ctx):
    ctx.llm.providers = [FakeProvider(lambda s, u: "мусор")]
    with pytest.raises(LLMError):
        ctx.llm.json("triage", "s", "u", TriageOut)


def test_budget_guard(ctx):
    from ideaminer.llm import Usage
    per_call = ctx.llm._cost("deepseek-flash", Usage(1000, 200, 0))  # the cost of one FakeProvider call
    assert per_call > 0
    ctx.cfg.budget.daily_usd = per_call * 2.5
    ctx.llm.providers = [FakeProvider(lambda s, u: '{"items": []}')]
    ctx.llm.json("triage", "s", "u", TriageOut)
    ctx.llm.json("triage", "s", "u", TriageOut)
    ctx.llm.json("triage", "s", "u", TriageOut)  # before the third call 2 × per_call is still under the limit
    with pytest.raises(BudgetExceeded):
        ctx.llm.json("triage", "s", "u", TriageOut)
