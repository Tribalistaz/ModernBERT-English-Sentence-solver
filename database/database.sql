-- Sentence Rebuilder Database Schema
-- MySQL/MariaDB

-- Create database
CREATE DATABASE IF NOT EXISTS sentence_rebuilder 
CHARACTER SET utf8mb4 
COLLATE utf8mb4_unicode_ci;

USE sentence_rebuilder;

-- Users table
CREATE TABLE IF NOT EXISTS users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    uuid VARCHAR(36) NOT NULL UNIQUE,
    username VARCHAR(50) NOT NULL UNIQUE,
    email VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100),
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP NULL,
    is_active BOOLEAN DEFAULT TRUE,
    INDEX idx_username (username),
    INDEX idx_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Prediction history table
CREATE TABLE IF NOT EXISTS prediction_history (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NULL,  -- NULL allows anonymous predictions
    input_sentence TEXT NOT NULL,
    reconstructed_sentence TEXT,
    predictions_json JSON,  -- Store full prediction data
    prediction_mode VARCHAR(10) DEFAULT 'best',
    prediction_type VARCHAR(20),  -- 'missing_words', 'missing_letters', 'mixed'
    processing_time_ms INT,
    success BOOLEAN DEFAULT TRUE,
    error_message TEXT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_user_id (user_id),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- User preferences table
CREATE TABLE IF NOT EXISTS user_preferences (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL UNIQUE,
    dark_mode BOOLEAN DEFAULT FALSE,
    default_mode VARCHAR(10) DEFAULT 'best',
    language VARCHAR(10) DEFAULT 'en',
    notifications_enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Sessions table (for session management)
CREATE TABLE IF NOT EXISTS user_sessions (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    session_token VARCHAR(255) NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ip_address VARCHAR(45),
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    INDEX idx_session_token (session_token),
    INDEX idx_user_id (user_id),
    INDEX idx_expires_at (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Login attempt tracking (rate limiting)
CREATE TABLE IF NOT EXISTS login_attempts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    identifier VARCHAR(120) NOT NULL,
    ip_address VARCHAR(45) NOT NULL,
    success BOOLEAN DEFAULT FALSE,
    attempted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_attempted_at (attempted_at),
    INDEX idx_identifier (identifier),
    INDEX idx_ip_address (ip_address)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS prediction_feedback (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    history_id INT NULL,
    user_id INT NULL,
    input_sentence TEXT NOT NULL,
    predicted_sentence TEXT NULL,
    corrected_sentence TEXT NULL,
    categories_json JSON NOT NULL,
    notes TEXT NULL,
    status VARCHAR(20) DEFAULT 'new',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (history_id) REFERENCES prediction_history(id) ON DELETE SET NULL,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    INDEX idx_feedback_history_id (history_id),
    INDEX idx_feedback_user_id (user_id),
    INDEX idx_feedback_status (status),
    INDEX idx_feedback_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Create a view for user statistics
CREATE OR REPLACE VIEW user_statistics AS
SELECT 
    u.id,
    u.uuid,
    u.username,
    u.email,
    u.is_admin,
    u.created_at,
    COUNT(ph.id) as total_predictions,
    COUNT(CASE WHEN ph.success = TRUE THEN 1 END) as successful_predictions,
    AVG(ph.processing_time_ms) as avg_processing_time,
    MAX(ph.created_at) as last_prediction_at
FROM users u
LEFT JOIN prediction_history ph ON u.id = ph.user_id
GROUP BY u.id, u.username, u.email, u.created_at;

SELECT 'Database schema created successfully!' as status;
