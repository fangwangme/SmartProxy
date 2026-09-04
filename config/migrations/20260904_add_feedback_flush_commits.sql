-- Idempotency ledger for retryable minute-aggregate flush transactions.
CREATE TABLE IF NOT EXISTS feedback_flush_commits (
    flush_id UUID PRIMARY KEY,
    committed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE feedback_flush_commits IS 'Idempotency ledger for retryable aggregate flush transactions.';
