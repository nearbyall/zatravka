# zatravka

[Русский](README.ru.md) | **English**

Automated discovery of product and business ideas in Reddit, X and Threads feeds. The system
decides what to read, filters out noise, hands promising posts to an LLM and sorts the ideas it
finds: B2B/B2C, sphere (games, cosmetics, business…), digital or physical product, monetization
model. It looks not only for explicit requests ("I wish someone would build…") but also for
non-obvious ideas inferred from pain points, workarounds and trends.

```
 feeds ─▶ collect ─▶ prefilter ─▶ LLM triage ─▶ comments ─▶ idea extraction ─▶ classification ─▶ dedup
  ▲                  (language,    (0–10, in      (for the     (explicit,          (market, sphere,   (clusters +
  │                   spam, signal  batches)       best posts)  implicit,           type, scores)      mentions)
  │                   phrases)                                  lateral)
  └──── direction planner ◀── direction yield ◀── the LLM explorer proposes new directions
```

The web UI and CLI messages are in Russian. Ideas are written in `output_language` (Russian by
default); evidence quotes stay in the original language.

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env           # add keys: at least DeepSeek or Replicate
.venv/bin/zatravka init        # database + check of sources, keys and balance
.venv/bin/zatravka run         # one full run (10–20 minutes with public Reddit RSS)
.venv/bin/zatravka serve       # UI at http://127.0.0.1:8610
```

Scheduled runs: `zatravka daemon`, or Docker:

```bash
docker compose up -d web          # UI only at 127.0.0.1:8610, spends no LLM budget
docker compose up -d --build      # UI + worker: scheduled runs (spends the budget)
docker compose logs -f worker
docker compose down               # stop everything; data stays in ./data
```

## Sources and access

Platforms are read only through legitimate channels: official APIs and public feeds. The system
never tries to get around blocks. The HTTP client sends an honest User-Agent, reads
`x-ratelimit-*` and `Retry-After` and waits for the limit to reset on its own.

| Source | Without keys | With keys | What you need |
|---|---|---|---|
| **Reddit** | ✓ public RSS (about 1 request per minute, no post scores) | OAuth API: 100 requests per minute, scores, comment counts, NSFW flag | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` (a *script* app at reddit.com/prefs/apps; Reddit may require new apps to be approved for API access) |
| **X** | ✗ | API v2, search over the last 7 days | `X_BEARER_TOKEN` (developer.x.com). Reads are paid, so `max_results` is small |
| **Threads** | ✓ web mode: public profiles from the account list and the first replies under posts (no keyword search without login) | Threads API: `keyword_search`, profiles via `profile_posts` | `THREADS_ACCESS_TOKEN` with `threads_basic` and `threads_keyword_search`. Until the app passes Meta review, search finds only your own posts |
| **RSS** | ✓ any RSS/Atom feeds | — | `sources.rss.feeds` in the config, e.g. your own RSSHub for X/Threads |
| **Files** | ✓ JSON, JSONL, CSV from `data/inbox/` | — | exports from third-party tools. The platform comes from the `platform` field or the file name prefix (`x_*.json`, `threads_*.csv`) |

Reddit via RSS and Threads in web mode work out of the box. X and the official Threads API turn
on by themselves once a token appears in `.env` (`enabled: auto`). Check access with
`zatravka doctor`.

### Threads without a token: web mode

Logged out, Threads shows public profiles but not search, so web mode reads the account list
from the registry in the database. Pages are opened by plain headless Chromium: no login, no
disguise, a 20-second pause between pages. Posts are taken from the JSON the page embeds in
itself, not from the markup. If Threads shows a login wall instead of a verified profile three
times in a row, the mode turns off until the end of the run; there are no workarounds. Automated
collection is against Meta's terms: turn the mode off with `sources.threads.web.enabled`. Once a
token appears, the source switches to the official API by itself (`sources.threads.mode: auto`).

```bash
zatravka accounts add @name1 @name2 --direction region_cis   # add accounts to a direction
zatravka accounts check @name                                # open the profile and show what is visible
zatravka accounts discover --direction cis_sellers --count 12   # the LLM suggests, the browser verifies
zatravka accounts expand --direction cis_sellers --count 10     # authors of related threads, no LLM
zatravka accounts list
```

The LLM often invents handles: of 12 candidates usually 1–3 pass and the rest fail verification.
Threads itself grows the registry more reliably. For the best posts of a run
(`comments_for_top_posts`) web mode opens the post page. It takes the first replies from there
(up to 5–8 are visible logged out) and five "related threads" that Threads picks by topic and
language. Authors of related threads join the post's direction, marked as found through related
threads, while the direction has fewer than `max_accounts_per_direction` enabled accounts. Turn
it off with `sources.threads.related_authors: false`. When seed accounts post mostly ads or news
and nothing passes triage, `accounts expand` opens the pages of the direction's best posts
regardless of the threshold and adopts their related authors the same way. An account that comes back empty three
times in a row is disabled automatically. A run reads `accounts_per_direction` accounts of each
direction (round robin) and `global_accounts_per_run` shared ones. Chromium is looked up in PATH
and the Playwright cache; the Docker image already includes it.

### Connecting X

1. Go to developer.x.com and create a Project with an App inside it.
2. Post search (`/2/tweets/search/recent`) needs paid access: pay-per-use or a plan. The free
   tier does not include search. Current prices are in the developer console.
3. App → *Keys and tokens* → *Bearer Token* → *Generate*. Add it to `.env`:
   `X_BEARER_TOKEN=...`
4. `zatravka doctor` checks the token and shows how many posts were read out of the project cap.
5. Spending is controlled in `config.yaml`: `sources.x.max_results` (10–100 posts per query),
   `queries_per_direction` and `daily_read_limit`. A run reads about "directions × queries ×
   max_results" posts, by default 6 × 1 × 10 = 60, which is ≈ $0.30 per run at $0.005 per post.
   X queries live in `directions.*.x_queries`.

### Connecting Threads

1. developers.facebook.com → *My Apps* → *Create app* → the *Access the Threads API* use case.
2. In the use case permissions enable `threads_basic` and `threads_keyword_search`. Reading
   other people's public profiles from `sources.threads.accounts` also needs
   `threads_profile_discovery`.
3. *App roles → Roles* → add your Threads account as a *Threads Tester*. Then accept the
   invitation in Threads: *Settings → Account → Website permissions → Invites*.
4. Get a short-lived token: in the app dashboard, Threads use case → user token generator (or
   OAuth via `threads.net/oauth/authorize`).
5. Exchange it for a long-lived one (60 days) and save it to `.env` with one command:
   ```bash
   zatravka threads-token <short_token> --app-secret <app_secret> --save
   ```
   The app secret is in *App settings → Basic → App secret* (the Threads use case has its own).
   After that zatravka refreshes the token itself: every three days, on each run.
6. Meta's limitation: until the app passes App Review for `threads_keyword_search`, search finds
   only posts of your own account. For real research submit the app for review (you will need
   a privacy policy and a screen recording).

Until you have keys, X and Threads can be fed with exports. Put JSON or CSV into `data/inbox/`
with a platform prefix (`x_2026-11.json`, `threads_export.csv`) and the next run will process
them. Use exports from tools you are allowed to use.

## LLM

- **DeepSeek** (`deepseek-flash`) is the main provider: OpenAI-compatible API, JSON mode,
  reasoning off (fast and cheap).
- **Replicate** (`openai/gpt-5-mini`) is the fallback: it kicks in when DeepSeek is unavailable
  (no key, out of credit, 401/402).
- Every response is validated against a pydantic schema. On a model error the model is shown
  its own error once.
- Every call is logged to `llm_calls` with tokens and cost. The daily and per-run budgets
  (`budget`) are hard: when one runs out the stage stops and the next run picks up the queue.
- Real spending (checked against balance deductions): a full run with 120 posts in triage,
  25 in extraction and about 80 ideas costs ≈ $0.09 (≈ 0.65 CNY).

> The default daily limit ($0.15) allows 1–2 runs a day. Raise `budget.daily_usd` for
> scheduled runs.

## How ideas are found

1. The **planner** takes the mandatory directions (`planner.core_directions`) plus
   `random_directions` random ones. Directions with better yield come up more often. Yield is
   the sum of idea scores above 5 per evaluated post, smoothed, and an `epsilon` share is pure
   chance. On top of that come a few random subreddits from `wildcard_pool` and a random feed
   type (top of the week or month, hot, new).
2. The **prefilter** (no LLM): language, length, age, spam patterns. Signal phrases in English
   and Russian ("I wish there was", "would pay for", "есть ли сервис", "вручную"…) push a post
   up the queue. 15% of the triage queue is random posts without signals: non-obvious ideas
   often hide there.
3. **LLM triage** in batches of 20 posts: a 0–10 score for how much idea material a post has,
   plus signals (wish, pain, workaround, spending, trend, profession, niche).
4. **Comments** for the best posts: "same here", "I pay for X, but…".
5. **Extraction**: up to 4 ideas per post, of three types: *explicit* (asked for directly),
   *implicit* (follows from the pain), *lateral* (lateral moves: "picks and shovels", a B2B
   version of a consumer pain, physical↔digital transfer, an adjacent audience with money,
   a second-order effect of a trend, inversion). Every idea has a verbatim evidence quote. The
   code checks that the quote really is in the post and flags unverified ones in the UI.
6. **Classification** against the taxonomy from the config: market, main and secondary spheres,
   product type, monetization model, tags and six scores (demand, money, novelty, feasibility,
   open niche, urgency). The final score is computed by code from `scoring.weights`, not by the
   model.
7. **Dedup**: a lexical index (TF-IDF over trigrams, tolerant to Russian word forms) finds
   candidates and an LLM judge decides the borderline pairs. A recurring idea becomes a cluster,
   and its mention count raises its score.
8. The **LLM explorer** looks at the statistics every `explore.every_n_runs` runs and proposes
   new directions: one deepens the most productive direction, the rest go into non-obvious
   niches from random seeds. Subreddits are checked for existence. Weak LLM directions are
   disabled automatically over time.

## Regions

The LLM gives every idea:
- **launch regions**: 1–3 markets where the idea can launch without a fundamental redesign:
  CIS, EU, North America, Latin America, Eastern Europe outside the EU, Middle East, Africa,
  South Asia, East Asia, Oceania or "global". If a product works somewhere but is missing in
  another region, the LLM adds that region too: transferring a model is also an opportunity;
- **signal origin**: the region where the pain was seen. Hints for the LLM: post language,
  community, mentioned services, laws and currencies, the direction's region.

The launch region filter is in the UI (with an "include global" checkbox), in the CLI
(`--region cis`, `--no-global`, `--origin north_america`) and in the Excel export (columns plus
a "sphere × region" sheet). The map on the main page switches between market, region and
product type.

Collection can be steered to a region too. `config.yaml` has regional directions (`region_cis`,
`region_eu`, `region_latam`, … with Russian, Spanish and Portuguese queries) and six CIS topic
directions (`cis_sellers`, `cis_smb_admin`, `cis_home_services`, `cis_parents_edu`,
`cis_daily_services`, `cis_freelance_it`) with X queries built on complaint vocabulary. Two
levers:
- `planner.boost_regions: {cis: 3}` (on by default): CIS directions come up three times as
  often, other regions stay in play;
- `planner.regions: [cis, eu]`: a hard focus. These regions' directions run every time and
  other regions' directions are not read.

The region list is in `taxonomy.regions`. After editing it ideas are relabeled automatically
over the next runs (or right away: `zatravka reclassify`).

## Configuration

Everything lives in `config.yaml`. The main parts:

- `directions`: subreddits, Reddit search, X queries (API v2 syntax), Threads keywords. A new
  direction is picked up on the next run. The daemon rereads the config every cycle.
- `taxonomy`: markets, spheres, product types, models. After editing run
  `zatravka reclassify` and the LLM relabels ideas for the new taxonomy.
- `scoring.weights`: score weights. After editing run `zatravka rescore` (no LLM).
- `prefilter.signals` and `spam_patterns`: weighted regular expressions.
- `triage.threshold`, `random_share`, `extract.max_posts_per_run`: strictness and volume.
  `triage.platform_thresholds` overrides the threshold per platform: short Threads posts pass at 5.
- `budget`, `schedule`: money and timing.
- Prompts live in `zatravka/prompts/*.md`. Edit them in place or point `storage.prompts_dir` at
  your own folder (you can override only the files you need).

One-off overrides without editing the file: `zatravka --set triage.max_posts_per_run=30 run`.

## Commands

```
zatravka init                      database, environment check
zatravka doctor                    sources, keys, DeepSeek balance, today's spending
zatravka run [--directions a,b] [--sources reddit] [--no-llm] [--explore] [--digest] [--seed N]
zatravka collect                   collection only, no LLM
zatravka process                   LLM processing of the queued posts, no collection
zatravka explore                   the LLM proposes new directions right now
zatravka ideas [--market B2B] [--sphere games] [--nature physical] [--insight lateral] [--region cis]
               [--platform reddit] [--q text] [--min-score 6] [--days 7] [--sort score|new|mentions|novelty|demand|feasibility]
zatravka export [--format xlsx|csv|json|md] [same filters] [--out file]   Excel by default
zatravka digest [--days 1] [--send]   Markdown digest in data/digests (+ Telegram)
zatravka import file.json [--direction key]
zatravka reclassify [--all]        relabel after a taxonomy change
zatravka rescore                   recompute scores after a weight change
zatravka stats                     database, direction and spending statistics
zatravka daemon                    scheduled runs + daily digest
zatravka serve [--host --port]     web UI
zatravka unlock                    release a stuck run lock
zatravka threads-token SHORT --app-secret SECRET [--save]   long-lived Threads token
zatravka accounts list|add|remove|check|discover|expand [@name…] [--direction key] [--count N]
```

The Excel export is a three-sheet workbook. "Ideas": every field, the six scores, readable
labels, autofilter, frozen header and title, a link to the post. "Sources": quotes marked by
whether they were found verbatim in the post, with links. "Sphere × market": a pivot table.
Social media texts are written as strings, so formulas from posts never execute (in CSV such
values are escaped with an apostrophe).

## Web UI

- **Ideas**: a "sphere × market" heatmap (clicking a cell filters the list), filters by product
  type, idea type, platform, status and score, search, export of the current selection to
  Excel, CSV, JSON or Markdown. Ideas can be starred or rejected right from the list.
- **Idea page**: problem, solution, audience, how it was derived from the post, why now,
  monetization, risks, the six scores, tags, quotes with links to posts, variants of the same
  idea, status (new / starred / in progress / rejected / archived), a note.
- **Posts**: everything that was read and what happened to it, for tuning the filters.
- **Directions**: each direction's yield, on/off switches, LLM proposals with their rationale.
- **Runs**: history, errors, LLM spending by day, feed health.
- The "run" button starts a run in the background and shows a live log. The daemon and the
  button never overlap thanks to a run lock in the database.

## Architecture

Layers depend only on the layers below them: `core` knows nothing about the database or the
network, `storage` knows nothing about the pipeline, and the CLI and web UI go through
repositories and contain no SQL.

```
zatravka/
  config.py        typed config + .env
  core/            logic without I/O: models, prefilter, text similarity, scoring,
                   taxonomy, selection, labels
  storage/         SQLite (WAL): connection, migrations and repositories — posts, ideas,
                   idea_queries (UI queries), directions, accounts, runs, feeds, usage
  net/             polite HTTP client and honest headless rendering (Chromium, no login, no disguise)
  llm/             DeepSeek and Replicate providers, JSON schemas, budget, cost tracking
  prompts/         stage prompts (Markdown, editable without code changes)
  sources/         reddit (RSS/OAuth), x (API v2), threads (api + web + auth), rss, inbox
  pipeline/        context, planner, stages (collect, triage, enrich, extract,
                   classify, dedup, explore, digest, discover), single run and daemon
  export/          export records and formats: Excel, CSV, JSON, Markdown
  cli/             commands by group: runs, ideas, accounts, setup
  web/             FastAPI + Jinja2: routers, background run, template helpers
```

A new source is a subclass of `sources.base.Source` with `status`, `plan` (or `plan_global`) and
`fetch`, registered in `sources/__init__.py`.

Tests: `.venv/bin/pytest`. The LLM and the network are mocked.
