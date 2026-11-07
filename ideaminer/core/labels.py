"""Russian labels for the UI, digests and exports."""

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
    """Short region label: a known abbreviation or the full label up to its parenthesis."""
    if not key:
        return "—"
    if key in REGION_SHORT:
        return REGION_SHORT[key]
    return (regions or {}).get(key, key).split(" (")[0]
