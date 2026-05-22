-- Миграция AI-qualifier для pending_review
-- Применяется через python wrapper с per-column try/except (idempotent).

ALTER TABLE pending_review ADD COLUMN ai_score INTEGER;
ALTER TABLE pending_review ADD COLUMN ai_pain TEXT;
ALTER TABLE pending_review ADD COLUMN ai_fit_service TEXT;
ALTER TABLE pending_review ADD COLUMN ai_reason TEXT;
ALTER TABLE pending_review ADD COLUMN ai_processed_at TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_pending_ai_status
  ON pending_review(status, id);

CREATE INDEX IF NOT EXISTS idx_pending_corpus_status
  ON pending_review(corpus_id, status);
