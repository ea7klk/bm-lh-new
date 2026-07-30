CREATE TABLE IF NOT EXISTS raw_events (
    id BIGSERIAL PRIMARY KEY,
    payload_hash TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    start_at TIMESTAMPTZ,
    stop_at TIMESTAMPTZ,
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS raw_events_received_at_idx
    ON raw_events (received_at DESC);

CREATE INDEX IF NOT EXISTS raw_events_session_id_idx
    ON raw_events (session_id);

CREATE TABLE IF NOT EXISTS qsos (
    session_id TEXT PRIMARY KEY,
    raw_event_id BIGINT NOT NULL REFERENCES raw_events(id),
    source_id BIGINT,
    source_call TEXT,
    source_name TEXT,
    destination_id BIGINT,
    destination_call TEXT,
    destination_name TEXT,
    context_id BIGINT,
    link_call TEXT,
    link_name TEXT,
    link_type_name TEXT,
    slot SMALLINT,
    master TEXT,
    talker_alias TEXT,
    rssi REAL,
    ber REAL,
    start_at TIMESTAMPTZ NOT NULL,
    stop_at TIMESTAMPTZ NOT NULL,
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    is_native_session_id BOOLEAN NOT NULL DEFAULT TRUE,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS qsos_start_at_idx
    ON qsos (start_at DESC);

CREATE INDEX IF NOT EXISTS qsos_source_id_start_at_idx
    ON qsos (source_id, start_at DESC);

CREATE INDEX IF NOT EXISTS qsos_destination_id_start_at_idx
    ON qsos (destination_id, start_at DESC);

CREATE INDEX IF NOT EXISTS qsos_context_id_start_at_idx
    ON qsos (context_id, start_at DESC);

CREATE TABLE IF NOT EXISTS service_heartbeats (
    service_name TEXT PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS service_heartbeats_last_seen_at_idx
    ON service_heartbeats (last_seen_at DESC);

CREATE TABLE IF NOT EXISTS talkgroups (
    id BIGSERIAL PRIMARY KEY,
    talkgroup_id BIGINT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    country TEXT NOT NULL DEFAULT 'XX',
    continent TEXT NOT NULL DEFAULT 'Other',
    full_country_name TEXT NOT NULL DEFAULT 'Other',
    last_updated TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS talkgroups_country_idx
    ON talkgroups (country);

CREATE INDEX IF NOT EXISTS talkgroups_continent_idx
    ON talkgroups (continent);

-- Keep raw_events as the complete audit trail, but retain only records that
-- can appear in the dashboard's displayed QSO views.
DELETE FROM qsos q
WHERE q.destination_id IS NULL
   OR q.destination_id = 9
   OR q.destination_id > 999999
   OR (
       NULLIF(BTRIM(q.destination_name), '') IS NULL
       AND NOT EXISTS (
           SELECT 1
           FROM talkgroups t
           WHERE t.talkgroup_id = q.destination_id
             AND NULLIF(BTRIM(t.name), '') IS NOT NULL
       )
   );

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    callsign TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    email_verified_at TIMESTAMPTZ
);

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified_at TIMESTAMPTZ;

-- Language is selected in the bm_lang cookie, never persisted in the user record.
ALTER TABLE users DROP COLUMN IF EXISTS locale;

CREATE INDEX IF NOT EXISTS users_callsign_idx
    ON users (callsign);

CREATE INDEX IF NOT EXISTS users_email_idx
    ON users (email);

CREATE TABLE IF NOT EXISTS user_sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS user_sessions_user_id_idx
    ON user_sessions (user_id);

CREATE INDEX IF NOT EXISTS user_sessions_expires_at_idx
    ON user_sessions (expires_at);

CREATE TABLE IF NOT EXISTS email_verification_tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS email_verification_tokens_user_id_idx
    ON email_verification_tokens (user_id);

CREATE INDEX IF NOT EXISTS email_verification_tokens_expires_at_idx
    ON email_verification_tokens (expires_at);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS password_reset_tokens_user_id_idx
    ON password_reset_tokens (user_id);

CREATE INDEX IF NOT EXISTS password_reset_tokens_expires_at_idx
    ON password_reset_tokens (expires_at);
