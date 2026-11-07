"""Русские подписи для интерфейса, дайджеста и выгрузок."""

NATURE = {"digital": "цифровой", "physical": "физический", "hybrid": "гибрид", "service": "услуга"}
INSIGHT = {"explicit": "явная", "implicit": "неявная", "lateral": "неочевидная"}
STATUS = {"new": "Новая", "starred": "Избранное", "in_progress": "В работе",
          "rejected": "Отклонена", "archived": "Архив"}
POST_STATUS = {"new": "новый", "candidate": "в очереди", "filtered": "отсеян", "skipped": "пустой",
               "promising": "перспективный", "extracted": "разобран", "error": "ошибка"}
ACCOUNT_ORIGIN = {"manual": "добавлен вручную", "config": "из config.yaml", "llm": "подобран ИИ",
                  "related": "из похожих тредов"}
PLATFORM = {"reddit": "Reddit", "x": "X", "threads": "Threads", "rss": "RSS", "import": "Импорт"}
SCORE_LABELS = {"demand": "Спрос", "monetization": "Деньги", "novelty": "Новизна",
                "feasibility": "Реализуемость", "whitespace": "Свободная ниша", "urgency": "Острота"}
REGION_SHORT = {"global": "глобально", "north_america": "Сев. Америка", "latam": "Лат. Америка", "eu": "ЕС",
                "cis": "СНГ", "east_europe": "Вост. Европа", "mena": "Бл. Восток", "africa": "Африка",
                "south_asia": "Юж. Азия", "east_asia": "Вост. Азия", "oceania": "Океания"}


def region_short(key: str | None, regions: dict[str, str] | None = None) -> str:
    """Короткая подпись региона: из словаря выше или начало полной подписи до скобки."""
    if not key:
        return "—"
    if key in REGION_SHORT:
        return REGION_SHORT[key]
    label = (regions or {}).get(key, key)
    return label.split(" (")[0]
