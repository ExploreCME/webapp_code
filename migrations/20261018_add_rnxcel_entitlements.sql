-- RNXcel access is provisioned only after a verified nursing purchase.
-- No default grants and no dependency on PAXcel billing or users.has_paid.
CREATE TABLE IF NOT EXISTS rnxcel_entitlements (
    owner_id VARCHAR(255) COLLATE utf8mb4_bin PRIMARY KEY,
    is_active TINYINT(1) NOT NULL DEFAULT 0,
    expires_at DATETIME(6) NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
