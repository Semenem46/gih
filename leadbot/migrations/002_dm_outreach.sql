-- Миграция 002: DM-Outreach режим
-- Idempotent — применяется через python wrapper с per-column try/except

-- Режим клиента: 'lead-finder' (как было) или 'dm-outreach' (для DM)
ALTER TABLE paid_clients ADD COLUMN mode TEXT DEFAULT 'lead-finder';

-- Готовый DM-текст от AI и username получателя
ALTER TABLE pending_review ADD COLUMN ai_dm_text TEXT;
ALTER TABLE pending_review ADD COLUMN ai_dm_username TEXT;
ALTER TABLE pending_review ADD COLUMN ai_dm_fit TEXT;
ALTER TABLE pending_review ADD COLUMN dm_sent_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_pending_dm_status ON pending_review(status, ai_dm_text);
