"""Schema migrations; PRAGMA user_version records how many have been applied."""

MIGRATIONS = [
    """
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL DEFAULT 'running',
        trigger TEXT,
        plan_json TEXT,
        stats_json TEXT,
        cost_usd REAL NOT NULL DEFAULT 0,
        error TEXT
    );

    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY,
        platform TEXT NOT NULL,
        ext_id TEXT NOT NULL,
        url TEXT,
        author TEXT,
        community TEXT,
        title TEXT,
        body TEXT,
        lang TEXT,
        created_at TEXT,
        fetched_at TEXT NOT NULL,
        score INTEGER,
        num_comments INTEGER,
        metrics_json TEXT,
        direction TEXT,
        feed TEXT,
        run_id INTEGER,
        content_hash TEXT,
        nsfw INTEGER NOT NULL DEFAULT 0,
        prefilter_score REAL,
        prefilter_reason TEXT,
        status TEXT NOT NULL DEFAULT 'new',
        triage_score REAL,
        triage_signals TEXT,
        triage_note TEXT,
        comments_json TEXT,
        processed_at TEXT,
        error TEXT,
        UNIQUE(platform, ext_id)
    );
    CREATE INDEX IF NOT EXISTS posts_status ON posts(status);
    CREATE INDEX IF NOT EXISTS posts_hash ON posts(content_hash);
    CREATE INDEX IF NOT EXISTS posts_direction ON posts(direction);

    CREATE TABLE IF NOT EXISTS ideas (
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        run_id INTEGER,
        title TEXT NOT NULL,
        one_liner TEXT,
        problem TEXT,
        solution TEXT,
        audience TEXT,
        insight_type TEXT,
        reasoning TEXT,
        why_now TEXT,
        monetization TEXT,
        risks TEXT,
        confidence REAL,
        market TEXT,
        sphere TEXT,
        spheres_json TEXT,
        nature TEXT,
        business_model TEXT,
        tags_json TEXT,
        scores_json TEXT,
        score_note TEXT,
        base_score REAL,
        score REAL,
        classified_at TEXT,
        taxonomy_version TEXT,
        cluster_id INTEGER,
        mentions INTEGER NOT NULL DEFAULT 1,
        dedup_done INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'new',
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS ideas_cluster ON ideas(cluster_id);
    CREATE INDEX IF NOT EXISTS ideas_score ON ideas(score);
    CREATE INDEX IF NOT EXISTS ideas_sphere ON ideas(sphere);

    CREATE TABLE IF NOT EXISTS idea_evidence (
        idea_id INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
        post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        quote TEXT,
        quote_verified INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (idea_id, post_id)
    );

    CREATE TABLE IF NOT EXISTS directions (
        key TEXT PRIMARY KEY,
        name TEXT,
        origin TEXT NOT NULL DEFAULT 'config',
        enabled INTEGER NOT NULL DEFAULT 1,
        weight REAL NOT NULL DEFAULT 1.0,
        spec_json TEXT NOT NULL,
        rationale TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_run_at TEXT,
        runs INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS llm_calls (
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        run_id INTEGER,
        stage TEXT,
        provider TEXT,
        model TEXT,
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        cached_tokens INTEGER,
        cost_usd REAL NOT NULL DEFAULT 0,
        duration_ms INTEGER,
        ok INTEGER NOT NULL DEFAULT 1,
        error TEXT
    );
    CREATE INDEX IF NOT EXISTS llm_calls_ts ON llm_calls(ts);

    CREATE TABLE IF NOT EXISTS feed_state (
        feed TEXT PRIMARY KEY,
        last_fetched_at TEXT,
        last_status TEXT,
        last_count INTEGER,
        new_count INTEGER,
        errors INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS kv (
        key TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # v2: regions
    """
    ALTER TABLE posts ADD COLUMN region_hint TEXT;
    ALTER TABLE ideas ADD COLUMN regions_json TEXT;
    ALTER TABLE ideas ADD COLUMN origin_region TEXT;
    """,
    # v3: registry of accounts read via profile pages
    """
    CREATE TABLE IF NOT EXISTS accounts (
        platform TEXT NOT NULL,
        username TEXT NOT NULL,
        direction TEXT,
        origin TEXT NOT NULL DEFAULT 'manual',
        enabled INTEGER NOT NULL DEFAULT 1,
        note TEXT,
        added_at TEXT NOT NULL,
        last_fetched_at TEXT,
        fetches INTEGER NOT NULL DEFAULT 0,
        empty_fetches INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (platform, username)
    );
    """,
    # v4: evidence lookup by post, for the platform filter
    """
    CREATE INDEX IF NOT EXISTS idea_evidence_post ON idea_evidence(post_id);
    """,
]
