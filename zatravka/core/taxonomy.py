from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config

Taxonomy = dict[str, dict[str, str]]

DEFAULT_TAXONOMY: Taxonomy = {
    "markets": {"B2B": "Бизнесу", "B2C": "Потребителю", "B2B2C": "Через бизнес к потребителю",
                "C2C": "Между людьми", "B2G": "Государству"},
    "natures": {"digital": "Цифровой продукт", "physical": "Физический товар",
                "hybrid": "Физическое + цифровое", "service": "Услуга"},
    "business_models": {"saas": "SaaS", "subscription": "Подписка", "freemium": "Freemium",
                        "marketplace": "Маркетплейс", "transactional": "Разовая продажа", "ads": "Реклама",
                        "hardware": "Устройства", "consumables": "Расходники", "service": "Услуги",
                        "licensing": "Лицензирование", "data_api": "Данные/API", "community": "Сообщество",
                        "other": "Другое"},
    "spheres": {"games": "Игры", "beauty": "Красота и косметика", "business_ops": "Бизнес-операции",
                "health": "Здоровье", "education": "Образование", "finance": "Финансы",
                "ecommerce": "E-commerce", "dev_tools": "Инструменты разработчика", "other": "Другое"},
    "regions": {"global": "Глобально", "north_america": "Северная Америка", "latam": "Латинская Америка",
                "eu": "ЕС и Европа", "cis": "СНГ", "east_europe": "Восточная Европа вне ЕС",
                "mena": "Ближний Восток", "africa": "Африка", "south_asia": "Южная Азия",
                "east_asia": "Восточная Азия", "oceania": "Океания"},
}


def resolve_taxonomy(cfg: "Config") -> Taxonomy:
    """The configured taxonomy, with built-in defaults for sections left empty."""
    return {name: getattr(cfg.taxonomy, name) or default for name, default in DEFAULT_TAXONOMY.items()}


def normalize_key(value: str, options: dict[str, str], fallback: str | None = None) -> str | None:
    """Maps a model answer to a taxonomy key. Models sometimes reply with a label
    ("СНГ", "Игры и гейминг") instead of the key."""
    v = (value or "").strip().lower()
    if not v:
        return fallback
    by_lower = {k.lower(): k for k in options}
    if v in by_lower:
        return by_lower[v]
    for key, label in options.items():
        label = label.lower()
        if v in (label, label.split(" (")[0]) or v in label.split(","):
            return key
    return fallback
