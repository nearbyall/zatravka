"""Typed settings loaded from config.yaml and .env.

Every section has defaults, so config.yaml only needs what differs. Relative
paths resolve against the config file's directory.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

PACKAGE_DIR = Path(__file__).resolve().parent


class Storage(BaseModel):
    db_path: str = "data/ideaminer.db"
    inbox_dir: str = "data/inbox"
    exports_dir: str = "data/exports"
    digests_dir: str = "data/digests"
    prompts_dir: str | None = None


class Http(BaseModel):
    user_agent: str = "ideaminer/0.1 (personal research of public posts)"
    timeout_s: float = 30
    max_retries: int = 3
    max_rate_wait_s: float = 120
    min_interval_s: dict[str, float] = Field(default_factory=lambda: {"default": 1.0})


class Schedule(BaseModel):
    run_every_minutes: int = 240
    jitter_minutes: int = 15
    digest_hour_utc: int = 6


class Budget(BaseModel):
    daily_usd: float = 0.15
    per_run_usd: float = 0.12


class DeepSeekCfg(BaseModel):
    base_url: str = "https://api.deepseek.com"
    models: dict[str, str] = Field(default_factory=dict)
    default_model: str = "deepseek-flash"

    def model_for(self, stage: str) -> str:
        return self.models.get(stage, self.default_model)


class ReplicateCfg(BaseModel):
    model: str = "openai/gpt-5-mini"
    reasoning_effort: str = "minimal"


class Price(BaseModel):
    in_: float = Field(0.0, alias="in")
    cached_in: float = 0.0
    out: float = 0.0


class LLMCfg(BaseModel):
    providers: list[str] = Field(default_factory=lambda: ["deepseek", "replicate"])
    concurrency: int = 4
    timeout_s: float = 120
    deepseek: DeepSeekCfg = Field(default_factory=DeepSeekCfg)
    replicate: ReplicateCfg = Field(default_factory=ReplicateCfg)
    prices_usd_per_mtok: dict[str, Price] = Field(default_factory=dict)


class RedditCfg(BaseModel):
    enabled: bool = True
    mode: str = "auto"
    listings: dict[str, float] = Field(default_factory=lambda: {"top_week": 3, "hot": 2, "new": 1})
    subs_per_feed: int = 4
    feeds_per_direction: int = 2
    searches_per_direction: int = 1
    items_per_feed: int = 50
    comments_for_top_posts: int = 8
    comments_per_post: int = 15


class XCfg(BaseModel):
    enabled: bool | str = "auto"
    queries_per_direction: int = 1
    max_results: int = 10
    daily_read_limit: int = 200
    query_suffix: str = "-is:retweet -is:reply"


class ThreadsWebCfg(BaseModel):
    enabled: bool = False
    browser: str | None = None
    no_sandbox: bool = False
    min_interval_s: float = 20
    render_budget_ms: int = 12000
    max_empty_in_row: int = 3


class ThreadsCfg(BaseModel):
    enabled: bool | str = "auto"
    mode: str = "auto"
    queries_per_direction: int = 1
    search_type: str = "TOP"
    limit: int = 25
    accounts: list[str] = Field(default_factory=list)
    accounts_per_direction: int = 2
    global_accounts_per_run: int = 3
    related_authors: bool = True
    max_accounts_per_direction: int = 25
    web: ThreadsWebCfg = Field(default_factory=ThreadsWebCfg)


class RssFeed(BaseModel):
    url: str
    platform: str = "rss"
    label: str | None = None
    direction: str = "wildcard"


class RssCfg(BaseModel):
    enabled: bool = True
    feeds: list[RssFeed] = Field(default_factory=list)


class InboxCfg(BaseModel):
    enabled: bool = True


class Sources(BaseModel):
    reddit: RedditCfg = Field(default_factory=RedditCfg)
    x: XCfg = Field(default_factory=XCfg)
    threads: ThreadsCfg = Field(default_factory=ThreadsCfg)
    rss: RssCfg = Field(default_factory=RssCfg)
    inbox: InboxCfg = Field(default_factory=InboxCfg)


class Planner(BaseModel):
    core_directions: list[str] = Field(default_factory=lambda: ["pain_hunting"])
    random_directions: int = 4
    wildcard_subreddits: int = 3
    epsilon: float = 0.25
    seed: int | None = None
    regions: list[str] = Field(default_factory=list)
    boost_regions: dict[str, float] = Field(default_factory=dict)


class Signal(BaseModel):
    pattern: str
    weight: float = 1.0


class Prefilter(BaseModel):
    min_chars: int = 80
    max_chars: int = 8000
    max_age_days: int = 30
    languages: list[str] = Field(default_factory=lambda: ["en", "ru", "uk"])
    min_reddit_score: int = 0
    spam_patterns: list[str] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)


class Triage(BaseModel):
    batch_size: int = 20
    max_posts_per_run: int = 120
    threshold: float = 6
    random_share: float = 0.15
    max_body_chars: int = 1500


class Extract(BaseModel):
    max_posts_per_run: int = 25
    max_share_per_direction: float = 0.4
    max_ideas_per_post: int = 4
    max_body_chars: int = 6000
    max_comment_chars: int = 2500


class Classify(BaseModel):
    batch_size: int = 12


class Dedup(BaseModel):
    candidate_threshold: float = 0.30
    auto_merge_threshold: float = 0.80
    llm_judge: bool = True


class Scoring(BaseModel):
    weights: dict[str, float] = Field(default_factory=lambda: {
        "demand": 0.30, "monetization": 0.20, "novelty": 0.15,
        "feasibility": 0.15, "whitespace": 0.10, "urgency": 0.10,
    })
    mention_bonus: float = 0.4
    good_idea_threshold: float = 6.0


class Explore(BaseModel):
    enabled: bool = True
    every_n_runs: int = 6
    new_directions: int = 2
    prune_min_triaged: int = 80
    prune_min_yield: float = 0.03


class Digest(BaseModel):
    top_n: int = 15
    min_score: float = 6.0
    telegram: bool = False


class Web(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8610


class Taxonomy(BaseModel):
    markets: dict[str, str] = Field(default_factory=dict)
    natures: dict[str, str] = Field(default_factory=dict)
    business_models: dict[str, str] = Field(default_factory=dict)
    spheres: dict[str, str] = Field(default_factory=dict)
    regions: dict[str, str] = Field(default_factory=dict)

    def version(self) -> str:
        """A short fingerprint that changes whenever the keys change."""
        sections = (self.markets, self.natures, self.business_models, self.spheres, self.regions)
        keys = "|".join(",".join(sorted(d)) for d in sections)
        return hashlib.sha1(keys.encode()).hexdigest()[:8]


class Direction(BaseModel):
    key: str = ""
    name: str = ""
    weight: float = 1.0
    enabled: bool = True
    subreddits: list[str] = Field(default_factory=list)
    reddit_search: list[str] = Field(default_factory=list)
    x_queries: list[str] = Field(default_factory=list)
    threads_queries: list[str] = Field(default_factory=list)
    threads_accounts: list[str] = Field(default_factory=list)
    search_scope: str = "subs"  # "subs" searches the direction's subreddits, "site" all of Reddit
    region: str | None = None  # a taxonomy.regions key, passed to the classifier as a hint
    note: str = ""


class Secrets(BaseModel):
    deepseek_api_key: str | None = None
    replicate_api_token: str | None = None
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_username: str | None = None
    x_bearer_token: str | None = None
    threads_access_token: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


class Config(BaseModel):
    output_language: str = "ru"
    storage: Storage = Field(default_factory=Storage)
    http: Http = Field(default_factory=Http)
    schedule: Schedule = Field(default_factory=Schedule)
    budget: Budget = Field(default_factory=Budget)
    llm: LLMCfg = Field(default_factory=LLMCfg)
    sources: Sources = Field(default_factory=Sources)
    planner: Planner = Field(default_factory=Planner)
    prefilter: Prefilter = Field(default_factory=Prefilter)
    triage: Triage = Field(default_factory=Triage)
    extract: Extract = Field(default_factory=Extract)
    classify: Classify = Field(default_factory=Classify)
    dedup: Dedup = Field(default_factory=Dedup)
    scoring: Scoring = Field(default_factory=Scoring)
    explore: Explore = Field(default_factory=Explore)
    digest: Digest = Field(default_factory=Digest)
    web: Web = Field(default_factory=Web)
    taxonomy: Taxonomy = Field(default_factory=Taxonomy)
    wildcard_pool: list[str] = Field(default_factory=list)
    directions: dict[str, Direction] = Field(default_factory=dict)

    # Set by load_config, never read from YAML
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)
    secrets: Secrets = Field(default_factory=Secrets, exclude=True)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def db_path(self) -> Path:
        return self.path(self.storage.db_path)

    @property
    def prompts_dir(self) -> Path:
        if self.storage.prompts_dir:
            return self.path(self.storage.prompts_dir)
        return PACKAGE_DIR / "prompts"


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE parser. Within the file the last non-empty value wins, so a key can be
    appended below an empty placeholder; variables already set in the environment are kept."""
    if not path.exists():
        return
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and value:
            values[key] = value
    for key, value in values.items():
        if not os.environ.get(key):
            os.environ[key] = value


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def read_secrets() -> Secrets:
    return Secrets(
        deepseek_api_key=_env("DEEPSEEK_API_KEY", "DEEPSEEK_TOKEN"),
        replicate_api_token=_env("REPLICATE_API_TOKEN"),
        reddit_client_id=_env("REDDIT_CLIENT_ID"),
        reddit_client_secret=_env("REDDIT_CLIENT_SECRET"),
        reddit_username=_env("REDDIT_USERNAME"),
        x_bearer_token=_env("X_BEARER_TOKEN", "TWITTER_BEARER_TOKEN"),
        threads_access_token=_env("THREADS_ACCESS_TOKEN"),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
    )


def find_config(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("IDEAMINER_CONFIG")
    if env:
        return Path(env).resolve()
    return (Path.cwd() / "config.yaml").resolve()


def load_config(path: str | Path | None = None, raw_override: dict[str, Any] | None = None) -> Config:
    cfg_path = find_config(str(path) if path else None)
    data: dict[str, Any] = {}
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if raw_override:
        data = _deep_merge(data, raw_override)
    base_dir = cfg_path.parent
    load_dotenv(base_dir / ".env")
    cfg = Config.model_validate(data)
    cfg.base_dir = base_dir
    for key, d in cfg.directions.items():
        d.key = key
        if not d.name:
            d.name = key
    cfg.secrets = read_secrets()
    return cfg


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
