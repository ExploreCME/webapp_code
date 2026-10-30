-- Apply before deploying the numbered PAXcel game.
-- Snapshots preserve the exact question and choices shown when the host pressed GO.
CREATE TABLE IF NOT EXISTS paxcel_game_questions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    question_id VARCHAR(64) NOT NULL,
    question_snapshot JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_game_bank_question (question_id)
) ENGINE=InnoDB AUTO_INCREMENT=1000 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS paxcel_game_responses (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
    game_question_id BIGINT UNSIGNED NOT NULL,
    question_id VARCHAR(64) NOT NULL,
    answer_choice ENUM('A', 'B', 'C', 'D', 'E') NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_game_response_distribution (game_question_id, answer_choice),
    INDEX idx_game_response_bank_question (question_id),
    CONSTRAINT fk_game_response_question FOREIGN KEY (game_question_id)
        REFERENCES paxcel_game_questions(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
