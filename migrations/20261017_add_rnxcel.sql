-- Nursing-only private content and immutable attempt snapshots.
-- No seed content: reviewed versions must be explicitly authored and published.
-- Treat version_id as immutable: revisions receive a new version_id, never
-- replace authored_json for an existing published version.
CREATE TABLE IF NOT EXISTS rnxcel_consents (
    owner_id VARCHAR(255) COLLATE utf8mb4_bin PRIMARY KEY,
    accepted_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rnxcel_item_versions (
    version_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    publication_status ENUM('draft', 'published', 'retired') NOT NULL DEFAULT 'draft',
    exam_profile VARCHAR(64) NOT NULL,
    authored_json JSON NOT NULL,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    published_at DATETIME(6) NULL,
    INDEX idx_rnxcel_publication (publication_status, exam_profile)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS rnxcel_attempts (
    id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
    owner_id VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
    snapshot_json JSON NOT NULL,
    responses_json JSON NOT NULL,
    current_index INT UNSIGNED NOT NULL DEFAULT 0,
    status ENUM('active', 'completed') NOT NULL DEFAULT 'active',
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at DATETIME(6) NULL,
    INDEX idx_rnxcel_owner_history (owner_id, created_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
