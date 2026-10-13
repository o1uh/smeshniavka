-- Схема базы данных SQLite «Смешнявки» (WAL mode, UTC время)

CREATE TABLE IF NOT EXISTS messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    user_id INTEGER,
    alias TEXT,
    nick TEXT,
    nick_history TEXT,
    sender_kind TEXT DEFAULT 'user',
    fwd_kind TEXT,
    fwd_origin_name TEXT,
    reply_to INTEGER,
    reply_to_user_id INTEGER,
    quote_text TEXT,
    via_bot TEXT,
    text TEXT,
    urls TEXT,
    media_kind TEXT,
    file_unique_id TEXT,
    is_bot INTEGER DEFAULT 0,
    from_other_bot INTEGER DEFAULT 0,
    edited_at TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_created ON messages (chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages (chat_id, user_id);
CREATE INDEX IF NOT EXISTS idx_messages_file_unique ON messages (file_unique_id);

CREATE TABLE IF NOT EXISTS dossiers (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    dossier_data TEXT NOT NULL, -- JSON по R4 (manner, interests, music_taste, relations, triggers, facts)
    version INTEGER DEFAULT 1,
    last_processed_seq INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL,
    was TEXT, -- JSON список устаревших фактов при противоречиях
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS chat_summary (
    chat_id INTEGER PRIMARY KEY,
    summary TEXT NOT NULL,
    msg_seq INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_cache (
    file_unique_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    transcript TEXT,
    ocr_text TEXT,
    summary TEXT NOT NULL,
    track_meta TEXT, -- JSON
    tags TEXT,       -- JSON list
    verdict TEXT NOT NULL,
    score100 INTEGER,
    score_reason TEXT,
    unintelligible INTEGER DEFAULT 0,
    injection_suspected INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_cache_accessed ON media_cache (last_accessed);

CREATE TABLE IF NOT EXISTS research_cache (
    cache_key TEXT PRIMARY KEY, -- norm_query_hash или norm_url_hash
    query TEXT,
    url TEXT,
    result_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_accessed TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_cache_accessed ON research_cache (last_accessed);

CREATE TABLE IF NOT EXISTS key_state (
    key_hash TEXT PRIMARY KEY,
    key_env TEXT NOT NULL,
    rpm_used INTEGER DEFAULT 0,
    rpd_used INTEGER DEFAULT 0,
    tpm_used INTEGER DEFAULT 0,
    rpm_limit INTEGER DEFAULT 15,
    rpd_limit INTEGER DEFAULT 500,
    tpm_limit INTEGER DEFAULT 250000,
    project TEXT,
    date_pt TEXT NOT NULL,
    cooldown_until REAL DEFAULT 0,
    errors INTEGER DEFAULT 0,
    is_dead INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS opt_out (
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS foreign_chats (
    chat_id INTEGER PRIMARY KEY,
    title TEXT,
    left_at TEXT NOT NULL
);
