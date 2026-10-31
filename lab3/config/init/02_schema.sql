\c runtime_sentinel

-- One row per monitored host
CREATE TABLE hosts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname       TEXT NOT NULL,
    cloud_provider TEXT,
    region         TEXT,
    registered_at  TIMESTAMPTZ DEFAULT now()
);

-- Runtime events — partitioned by time for write performance
CREATE TABLE runtime_events (
    id            UUID DEFAULT gen_random_uuid(),
    event_id      TEXT NOT NULL,
    host_id       UUID REFERENCES hosts(id),
    source_type   TEXT NOT NULL,
    library_name  TEXT,
    severity      INT DEFAULT 0,
    raw_payload   JSONB,
    captured_at   TIMESTAMPTZ NOT NULL,
    received_at   TIMESTAMPTZ DEFAULT now()
) PARTITION BY RANGE (captured_at);

-- Current month partition — all new writes land here
CREATE TABLE runtime_events_current
    PARTITION OF runtime_events
    FOR VALUES FROM (DATE_TRUNC('month', NOW()))
              TO   (DATE_TRUNC('month', NOW()) + INTERVAL '1 month');

-- Default partition
CREATE TABLE runtime_events_default
    PARTITION OF runtime_events DEFAULT;

-- Idempotency table — tracks which events have already been processed
CREATE TABLE processed_events (
    event_id       TEXT PRIMARY KEY,
    processed_at   TIMESTAMPTZ DEFAULT now(),
    consumer_group TEXT NOT NULL
);

-- Partial index covering only critical unresolved events
-- Dashboard queries use this — intentionally small and fast
CREATE INDEX idx_events_critical
    ON runtime_events (captured_at DESC, severity)
    WHERE severity >= 4;

GRANT ALL ON ALL TABLES    IN SCHEMA public TO app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app;

-- Seed data
INSERT INTO hosts (hostname, cloud_provider, region) VALUES
    ('host-dev-001', 'aws',   'eu-west-1'),
    ('host-dev-002', 'gcp',   'us-central1'),
    ('host-dev-003', 'azure', 'westeurope');