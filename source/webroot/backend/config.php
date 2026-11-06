<?php
/**
 * Database Configuration
 * Centralized database connection management
* config.php is doing: config loading, security utilities, and auth utilities
* sanitizeInput() for display safety
* SESSION_COOKIE_SECURE should be 1 in production HTTPS
* CORS is strict-allowlist and rejects unknown origins early
* CSRF is header-based and tied to session token lifecycle
 */

/**
 * Load .env key-value pairs into process environment.
 */
function loadEnvFile($path) {
    if (!is_file($path)) {
        return;
    }
    //loops through lines, trims wihespace, skips empty comment lines
    $lines = file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    if ($lines === false) {
        return;
    }
    //read fiel lines
    foreach ($lines as $line) {
        $line = trim($line);
        if ($line === '' || str_starts_with($line, '#')) {
            continue;
        }

        $pos = strpos($line, '=');
        if ($pos === false) {
            continue;
        }

        $key = trim(substr($line, 0, $pos));
        $value = trim(substr($line, $pos + 1));
// remove wrapping quotes from values
        if (
            (str_starts_with($value, '"') && str_ends_with($value, '"')) ||
            (str_starts_with($value, "'") && str_ends_with($value, "'"))
        ) {
            $value = substr($value, 1, -1);
        }
    //env set only if key isn't is not already defined
        if ($key !== '' && getenv($key) === false) {
            putenv($key . '=' . $value);
            $_ENV[$key] = $value;
            $_SERVER[$key] = $value;
        }
    }
}

/**
 * Get env value with fallback
 */
function envValue($key, $default = null) {
    $value = getenv($key);
    if ($value === false || $value === '') {
        return $default;
    }
    return $value;
}

// Load .env from project root
loadEnvFile(dirname(__DIR__) . '/.env');

define('APP_ENV', envValue('APP_ENV', 'production'));

// Database credentials
define('DB_HOST', envValue('DB_HOST', 'localhost'));
define('DB_PORT', envValue('DB_PORT', '3306'));
define('DB_NAME', envValue('DB_NAME', 'sentence_rebuilder'));
define('DB_USER', envValue('DB_USER', ''));
define('DB_PASS', envValue('DB_PASS', ''));
define('DB_CHARSET', 'utf8mb4');

// Session configuration
define('SESSION_LIFETIME', 3600 * 24 * 7); // 7 days
define('SESSION_NAME', 'sentence_rebuilder_session');
define('SESSION_COOKIE_SECURE', filter_var(envValue('SESSION_COOKIE_SECURE', '0'), FILTER_VALIDATE_BOOLEAN) ? 1 : 0);

// CORS / runtime security
define('CORS_ALLOWED_ORIGINS', envValue('CORS_ALLOWED_ORIGINS', 'http://localhost:8080,http://127.0.0.1:8080'));
define('CORS_ALLOW_CREDENTIALS', filter_var(envValue('CORS_ALLOW_CREDENTIALS', '1'), FILTER_VALIDATE_BOOLEAN) ? 1 : 0);
define('LOGIN_MAX_ATTEMPTS', intval(envValue('LOGIN_MAX_ATTEMPTS', '5')));
define('LOGIN_WINDOW_SECONDS', intval(envValue('LOGIN_WINDOW_SECONDS', '900')));
define('CSRF_TOKEN_TTL_SECONDS', intval(envValue('CSRF_TOKEN_TTL_SECONDS', '7200')));
define('BACKEND_EXEC_TIMEOUT_SECONDS', intval(envValue('BACKEND_EXEC_TIMEOUT_SECONDS', '180')));

// Security
define('PASSWORD_HASH_ALGO', PASSWORD_BCRYPT);
define('PASSWORD_HASH_COST', 12);

/**
 * Get database connection
 * @return PDO
 */
function getDB() {
    static $pdo = null;
    
    if ($pdo === null) {
        try {
            if (DB_USER === '') {
                throw new RuntimeException('DB_USER is empty. Set DB credentials in .env');
            }

            $dsn = "mysql:host=" . DB_HOST . ";port=" . DB_PORT . ";dbname=" . DB_NAME . ";charset=" . DB_CHARSET;
            $options = [
                PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
                PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
                PDO::ATTR_EMULATE_PREPARES => false,
            ];
            
            $pdo = new PDO($dsn, DB_USER, DB_PASS, $options);
        } catch (Throwable $e) {
            error_log("Database connection failed: " . $e->getMessage());
            die(json_encode([
                'success' => false,
                'error' => 'Database connection failed'
            ]));
        }
    }
    
    return $pdo;
}

//connection start
function startSecureSession() {
    if (session_status() === PHP_SESSION_NONE) {
        ini_set('session.cookie_httponly', 1);
        ini_set('session.use_only_cookies', 1);
        ini_set('session.use_strict_mode', 1);
        ini_set('session.cookie_secure', SESSION_COOKIE_SECURE);
        ini_set('session.cookie_samesite', 'Lax');
        session_name(SESSION_NAME);
        session_start();
    }
}

/**
 * Apply strict CORS policy based on configured allowed origins
 */
function sendCorsHeaders($allowedMethods = ['GET', 'POST']) {
    $origin = $_SERVER['HTTP_ORIGIN'] ?? '';
    $allowed = array_filter(array_map('trim', explode(',', CORS_ALLOWED_ORIGINS)));

    if ($origin !== '') {
        if (in_array($origin, $allowed, true)) {
            header('Access-Control-Allow-Origin: ' . $origin);
            header('Vary: Origin');
            if (CORS_ALLOW_CREDENTIALS) {
                header('Access-Control-Allow-Credentials: true');
            }
        } else {
            http_response_code(403);
            header('Content-Type: application/json');
            echo json_encode([
                'success' => false,
                'error' => 'Origin not allowed'
            ]);
            exit;
        }
    }

    header('Access-Control-Allow-Methods: ' . implode(', ', $allowedMethods));
    header('Access-Control-Allow-Headers: Content-Type, X-Requested-With, X-CSRF-Token');

    if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') {
        http_response_code(204);
        exit;
    }
}

 // Python command for exec() calls
function getPythonCommand() {
    $configured = trim((string) envValue('PYTHON_BIN', ''));
    if ($configured !== '') {
        return $configured;
    }
    return (strtoupper(substr(PHP_OS, 0, 3)) === 'WIN') ? 'python' : 'python3';
}

/**
 * Check whether a path is absolute on Windows or Unix-like systems
 */
function isAbsolutePath($path) {
    if (!is_string($path) || $path === '') {
        return false;
    }
    return preg_match('/^[A-Za-z]:[\\\\\\/]/', $path) === 1 || str_starts_with($path, '/') || str_starts_with($path, '\\\\');
}

/**
 * Resolve a Python artifact path (script/client/dictionary) with deployment-aware fallbacks
 * Returns first existing path, or the highest-priority candidate if none exist
 */
function resolvePythonArtifact($envKey, $defaultFileName) {
    $envPath = trim((string) envValue($envKey, ''));
    $candidates = [];

    if ($envPath !== '') {
        $candidates[] = isAbsolutePath($envPath)
            ? $envPath
            : dirname(__DIR__) . DIRECTORY_SEPARATOR . ltrim($envPath, '\\/');
    }

    $backendDir = __DIR__;
    $sourceDir = dirname(__DIR__, 2);

    $candidates[] = $backendDir . DIRECTORY_SEPARATOR . $defaultFileName;
    $candidates[] = $sourceDir . DIRECTORY_SEPARATOR . 'python' . DIRECTORY_SEPARATOR . $defaultFileName;

    foreach ($candidates as $candidate) {
        if (is_file($candidate)) {
            return $candidate;
        }
    }

    return $candidates[0];
}

// check whether user is loged in
 // @return array|false User data or false


function getCurrentUser() {
    startSecureSession();
    
    if (!isset($_SESSION['user_id'])) {
        return false;
    }
    
    // Verify session in database
    $db = getDB();
    $stmt = $db->prepare("
        SELECT u.*, us.expires_at 
        FROM users u
        JOIN user_sessions us ON u.id = us.user_id
        WHERE u.id = ? AND us.session_token = ? AND us.expires_at > NOW()
    ");
    $stmt->execute([$_SESSION['user_id'], $_SESSION['session_token'] ?? '']);
    
    $user = $stmt->fetch();
    
    if (!$user) {
        // Invalid session
        session_destroy();
        return false;
    }
    
    return $user;
}

//authentication required function
function requireAuth() {
    $user = getCurrentUser();
    if (!$user) {
        http_response_code(401);
        die(json_encode([
            'success' => false,
            'error' => 'Authentication required',
            'code' => 'AUTH_REQUIRED'
        ]));
    }
    return $user;
}

/**
 * Generate secure random token
 * @return string
 */
function generateToken($length = 64) {
    return bin2hex(random_bytes($length / 2));
}

/**
 * Get or create CSRF token stored in user session
 */
function getCsrfToken() {
    startSecureSession();
    $now = time();
    $token = $_SESSION['csrf_token'] ?? null;
    $issuedAt = intval($_SESSION['csrf_token_issued_at'] ?? 0);

    if (!$token || ($issuedAt > 0 && ($now - $issuedAt) > CSRF_TOKEN_TTL_SECONDS)) {
        $token = bin2hex(random_bytes(32));
        $_SESSION['csrf_token'] = $token;
        $_SESSION['csrf_token_issued_at'] = $now;
    }

    return $token;
}

 //Validate CSRF token against current session
function isValidCsrfToken($token) {
    startSecureSession();
    if (!is_string($token) || $token === '') {
        return false;
    }
    $sessionToken = $_SESSION['csrf_token'] ?? '';
    if (!is_string($sessionToken) || $sessionToken === '') {
        return false;
    }
    return hash_equals($sessionToken, $token);
}

 //Enforce CSRF token for state-changing requests, applies to post, put, patch, delete
 //reads token from X-CFSR token header with JSON 403 invalud token return
function requireCsrfToken() {
    $method = strtoupper($_SERVER['REQUEST_METHOD'] ?? 'GET');
    if (!in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'], true)) {
        return;
    }

    $token = $_SERVER['HTTP_X_CSRF_TOKEN'] ?? '';

    if (!isValidCsrfToken($token)) {
        http_response_code(403);
        echo json_encode([
            'success' => false,
            'error' => 'Invalid CSRF token',
            'code' => 'CSRF_INVALID'
        ]);
        exit;
    }
}

//Increase execution timeout for long-running model inference calls
function applyBackendExecutionTimeout() {
    $seconds = BACKEND_EXEC_TIMEOUT_SECONDS;
    if ($seconds > 0) {
        @set_time_limit($seconds);
        @ini_set('max_execution_time', (string) $seconds);
    }
}

/**
 *generate UUID v4 fro mrandom bytes
 *@return string
 */
function generateUuidV4() {
    $data = random_bytes(16);
    $data[6] = chr((ord($data[6]) & 0x0f) | 0x40);
    $data[8] = chr((ord($data[8]) & 0x3f) | 0x80);
    return vsprintf('%s%s-%s-%s-%s-%s%s%s', str_split(bin2hex($data), 4));
}

/**
 * input sanitation
 * @param string $input
 * @return string
 */
function sanitizeInput($input) {
    return htmlspecialchars(strip_tags(trim($input)), ENT_QUOTES, 'UTF-8');
}

/**
 * email validation
 * @param string $email
 * @return bool
 */
function validateEmail($email) {
    return filter_var($email, FILTER_VALIDATE_EMAIL) !== false;
}

/**
 * username validation
 * @param string $username
 * @return bool
 */
function validateUsername($username) {
    return preg_match('/^[a-zA-Z0-9_]{3,50}$/', $username);
}

/**
 * password strength validation
 * minimum 6 characters, number, special character
 * @param string $password
 * @return array ['valid' -> bool, 'message' -> string]
 */
function validatePassword($password) {
    if (strlen($password) < 6) {
        return ['valid' => false, 'message' => 'Password must be at least 6 characters'];
    }
    if (!preg_match('/[0-9]/', $password)) {
        return ['valid' => false, 'message' => 'Password must contain at least one number'];
    }
    // exclude a subset of specified special characters from allowed password set
    if (preg_match('/[`~,\'<>"]/', $password)) {
        return ['valid' => false, 'message' => 'Password cannot include any of these characters: ` ~ , \' < > "'];
    }
    if (!preg_match('/[^a-zA-Z0-9\s`~,\'<>"]/', $password)) {
        return ['valid' => false, 'message' => 'Password must contain at least one special character (excluding ` ~ , \' < > ")'];
    }
    return ['valid' => true, 'message' => 'Password is valid'];
}
?>
