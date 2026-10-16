-- Publish complete standardized versions independently of user-owned quizzes.
-- Apply before deploying version selection or running generate_standardized_quizzes.py.
CREATE TABLE IF NOT EXISTS standardized_quiz_versions (
    quiz_name VARCHAR(255) PRIMARY KEY,
    category VARCHAR(255) NOT NULL,
    question_ids JSON NOT NULL,
    created_date DATETIME NOT NULL,
    INDEX idx_standardized_category (category)
) ENGINE=InnoDB;
