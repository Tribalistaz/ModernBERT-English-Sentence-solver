<?php
/**
 * Authentication API
 * Handles registration, login, logout, and session management
 */

//loads shared config for DB connection, session helper, CSRF helpfers, constants, etc
require_once 'config.php';
// forces JSON response content type for all routes
header('Content-Type: application/json');
sendCorsHeaders(['GET', 'POST', 'OPTIONS']);

// Get request method and action, applying CORS policy
$method = $_SERVER['REQUEST_METHOD']; //right now not used
$action = $_GET['action'] ?? '';
define('ADMIN_EMAIL', strtolower(trim((string) envValue('ADMIN_EMAIL', ''))));

// main dispatcher route to appropriate handler
switch ($action) {
    case 'csrf':
        handleGetCsrf();
        break;
    case 'register':
        handleRegister();
        break;
    case 'login':
        handleLogin();
        break;
    case 'logout':
        handleLogout();
        break;
    case 'check':
        handleCheckAuth();
        break;
    case 'profile':
        handleGetProfile();
        break;
    case 'delete_profile':
        handleDeleteProfile();
        break;
    default:
        echo json_encode(['success' => false, 'error' => 'Invalid action']);
}

//function for handling user registration
function handleRegister() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'POST required']);
        return;
    }
    requireCsrfToken(); //before proceeding it requires a CSFR token
    
    $input = json_decode(file_get_contents('php://input'), true); //JSON parser
    //ahtnetication fields
    $username = sanitizeInput($input['username'] ?? ''); 
    $email = sanitizeInput($input['email'] ?? '');
    $password = $input['password'] ?? '';
    $fullName = sanitizeInput($input['full_name'] ?? '');
    
    // Validate inputs
    if (empty($username) || empty($email) || empty($password)) {
        echo json_encode(['success' => false, 'error' => 'All fields required']);
        return;
    }
    
    if (!validateUsername($username)) {
        echo json_encode(['success' => false, 'error' => 'Invalid username format (3-50 alphanumeric characters)']);
        return;
    }
    
    if (!validateEmail($email)) {
        echo json_encode(['success' => false, 'error' => 'Invalid email format']);
        return;
    }
    
    $passwordCheck = validatePassword($password);
    if (!$passwordCheck['valid']) {
        echo json_encode(['success' => false, 'error' => $passwordCheck['message']]);
        return;
    }
    
    $db = getDB();
    
    // Check whether username exists or not
    $stmt = $db->prepare("SELECT id FROM users WHERE username = ?");
    $stmt->execute([$username]);
    if ($stmt->fetch()) {
        echo json_encode(['success' => false, 'error' => 'Username already exists']);
        return;
    }
    
    // Check whether email exists or not
    $stmt = $db->prepare("SELECT id FROM users WHERE email = ?");
    $stmt->execute([$email]);
    if ($stmt->fetch()) {
        echo json_encode(['success' => false, 'error' => 'Email already registered']);
        return;
    }
    
    // Hash password using configurable algo
    $passwordHash = password_hash($password, PASSWORD_HASH_ALGO, ['cost' => PASSWORD_HASH_COST]);
    $userUuid = generateUuidV4();
    
    $grantAdmin = ADMIN_EMAIL !== '' && strcasecmp($email, ADMIN_EMAIL) === 0;
    $hasAdminColumn = hasUsersIsAdminColumn($db);

    // Insert/generates user
    try {
        if ($hasAdminColumn) {
            $stmt = $db->prepare("
                INSERT INTO users (uuid, username, email, password_hash, full_name, is_admin) 
                VALUES (?, ?, ?, ?, ?, ?)
            ");
            $stmt->execute([$userUuid, $username, $email, $passwordHash, $fullName, $grantAdmin ? 1 : 0]);
        } else {
            $stmt = $db->prepare("
                INSERT INTO users (uuid, username, email, password_hash, full_name) 
                VALUES (?, ?, ?, ?, ?)
            ");
            $stmt->execute([$userUuid, $username, $email, $passwordHash, $fullName]);
        }
        
        $userId = $db->lastInsertId();
        
        // Create default preferences
        $stmt = $db->prepare("INSERT INTO user_preferences (user_id) VALUES (?)");
        $stmt->execute([$userId]);
        
        echo json_encode([
            'success' => true,
            'message' => 'Registration successful',
            'user_id' => $userId,
            'user_uuid' => $userUuid,
            'is_admin' => $grantAdmin
        ]);
    } catch (PDOException $e) {
        error_log("Registration error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Registration failed']);
    }
}

//function for handling user login
function handleLogin() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'POST required']);
        return;
    }
    requireCsrfToken();
    
    $input = json_decode(file_get_contents('php://input'), true);
    
    $username = sanitizeInput($input['username'] ?? '');
    $password = $input['password'] ?? '';
    
    if (empty($username) || empty($password)) {
        echo json_encode(['success' => false, 'error' => 'Username and password required']);
        return;
    }
    
    $db = getDB();
    $clientIp = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
    $identifier = strtolower(trim($username));

    $rateLimit = isLoginRateLimited($db, $identifier, $clientIp);
    if ($rateLimit['limited']) {
        http_response_code(429);
        echo json_encode([
            'success' => false,
            'error' => 'Too many login attempts. Please try again later.',
            'retry_after_seconds' => $rateLimit['retry_after_seconds']
        ]);
        return;
    }
    
    // Find user in database
    $stmt = $db->prepare("SELECT * FROM users WHERE username = ? OR email = ?");
    $stmt->execute([$username, $username]);
    $user = $stmt->fetch();
    
    if (!$user) {
        recordLoginAttempt($db, $identifier, $clientIp, false);
        echo json_encode(['success' => false, 'error' => 'Invalid credentials']);
        return;
    }
    
    // Verify password with database
    if (!password_verify($password, $user['password_hash'])) {
        recordLoginAttempt($db, $identifier, $clientIp, false);
        echo json_encode(['success' => false, 'error' => 'Invalid credentials']);
        return;
    }
    
    if (!$user['is_active']) {
        echo json_encode(['success' => false, 'error' => 'Account is deactivated']);
        return;
    }

    // Enforce admin rights for the designated account if schema supports it.
    if (
        ADMIN_EMAIL !== '' &&
        strcasecmp((string)($user['email'] ?? ''), ADMIN_EMAIL) === 0 &&
        hasUsersIsAdminColumn($db) &&
        empty($user['is_admin'])
    ) {
        try {
            $stmt = $db->prepare("UPDATE users SET is_admin = 1 WHERE id = ?");
            $stmt->execute([$user['id']]);
            $user['is_admin'] = 1;
        } catch (Throwable $e) {
            error_log('Failed to set admin flag: ' . $e->getMessage());
        }
    }
    
    // Create and open user session
    startSecureSession();
    session_regenerate_id(true);
    $sessionToken = generateToken();
    $expiresAt = date('Y-m-d H:i:s', time() + SESSION_LIFETIME);
    
    // Store session in database
    try {
        $stmt = $db->prepare("
            INSERT INTO user_sessions (user_id, session_token, expires_at, ip_address, user_agent)
            VALUES (?, ?, ?, ?, ?)
        ");
        $stmt->execute([
            $user['id'],
            $sessionToken,
            $expiresAt,
            $_SERVER['REMOTE_ADDR'] ?? null,
            $_SERVER['HTTP_USER_AGENT'] ?? null
        ]);
        
        // Update last login
        $stmt = $db->prepare("UPDATE users SET last_login = NOW() WHERE id = ?");
        $stmt->execute([$user['id']]);
        
        // Set session variables
        $_SESSION['user_id'] = $user['id'];
        $_SESSION['username'] = $user['username'];
        $_SESSION['session_token'] = $sessionToken;
        recordLoginAttempt($db, $identifier, $clientIp, true);
        clearLoginFailures($db, $identifier, $clientIp);
        
        echo json_encode([
            'success' => true,
            'message' => 'Login successful',
            'user' => getUserPublicPayload($user)
        ]);
    } catch (PDOException $e) {
        error_log("Login error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Login failed']);
    }
}
//record login attempts with identifier and IP
function recordLoginAttempt(PDO $db, string $identifier, string $ipAddress, bool $success): void {
    try {
        $stmt = $db->prepare("
            INSERT INTO login_attempts (identifier, ip_address, success)
            VALUES (?, ?, ?)
        ");
        $stmt->execute([$identifier, $ipAddress, $success ? 1 : 0]);
    } catch (Throwable $e) {
        error_log('Login attempt tracking failed: ' . $e->getMessage());
    }
}

function clearLoginFailures(PDO $db, string $identifier, string $ipAddress): void {
    try {
        $stmt = $db->prepare("
            DELETE FROM login_attempts
            WHERE success = 0 AND (identifier = ? OR ip_address = ?)
        ");
        $stmt->execute([$identifier, $ipAddress]);
    } catch (Throwable $e) {
        error_log('Clearing login failures failed: ' . $e->getMessage()); //delete failed attempts by identifier or IP after successful login
    }
}

/**
 * Count failed attempts within window for identifier/IP
 * If failures < LOGIN_MAX_ATTEMPTS -> not limited
 * Else compute retry seconds from first failure timestamp
 * Return structured array: ['limited' => bool, 'retry_after_seconds' => int]
 * On error: fail-open (not limited), with logged error
 */
function isLoginRateLimited(PDO $db, string $identifier, string $ipAddress): array {
    try {
        $cutoff = date('Y-m-d H:i:s', time() - LOGIN_WINDOW_SECONDS);
        $stmt = $db->prepare("
            SELECT COUNT(*) AS failures, MIN(attempted_at) AS first_attempt
            FROM login_attempts
            WHERE success = 0
              AND attempted_at >= ?
              AND (identifier = ? OR ip_address = ?)
        ");
        $stmt->execute([$cutoff, $identifier, $ipAddress]);
        $row = $stmt->fetch();
        $failures = intval($row['failures'] ?? 0);

        if ($failures < LOGIN_MAX_ATTEMPTS) {
            return ['limited' => false, 'retry_after_seconds' => 0];
        }

        $firstAttempt = strtotime($row['first_attempt'] ?? '') ?: time();
        $retryAfter = max(1, LOGIN_WINDOW_SECONDS - (time() - $firstAttempt));
        return ['limited' => true, 'retry_after_seconds' => $retryAfter];
    } catch (Throwable $e) {
        error_log('Login rate-limit check failed: ' . $e->getMessage());
        return ['limited' => false, 'retry_after_seconds' => 0];
    }
}

//haldes user logout
function handleLogout() {
    requireCsrfToken(); //requires a CSFR token
    startSecureSession();
    
    if (isset($_SESSION['session_token'])) {
        $db = getDB();
        // Delete session from database
        $stmt = $db->prepare("DELETE FROM user_sessions WHERE session_token = ?");
        $stmt->execute([$_SESSION['session_token']]);
    }
    
    session_destroy();
    //return logout success JSON
    echo json_encode([
        'success' => true,
        'message' => 'Logged out successfully'
    ]);
}

//check authentication status if true return user details
function handleCheckAuth() {
    $user = getCurrentUser();
    
    if ($user) {
        echo json_encode([
            'success' => true,
            'authenticated' => true,
            'user' => getUserPublicPayload($user)
        ]);
    } else {
        echo json_encode([
            'success' => true,
            'authenticated' => false
        ]);
    }
}

//fetch user profile
function handleGetProfile() {
    $user = requireAuth(); //ensures only logged-in users access this
    
    $db = getDB();
    $adminSelect = hasUsersIsAdminColumn($db)
        ? 'u.is_admin'
        : "CASE WHEN LOWER(u.email) = '" . strtolower(ADMIN_EMAIL) . "' THEN 1 ELSE 0 END AS is_admin";
    $stmt = $db->prepare("
        SELECT
            u.id,
            u.uuid,
            u.username,
            u.email,
            u.full_name,
            u.created_at,
            u.last_login,
            u.is_active,
            {$adminSelect},
            up.dark_mode,
            up.default_mode,
            (SELECT COUNT(*) FROM prediction_history WHERE user_id = u.id) AS total_predictions,
            (SELECT AVG(processing_time_ms) FROM prediction_history WHERE user_id = u.id AND success = 1) AS avg_processing_time_ms
        FROM users u
        LEFT JOIN user_preferences up ON u.id = up.user_id
        WHERE u.id = ?
    ");
    $stmt->execute([$user['id']]);
    $profile = $stmt->fetch();
    //Executes query for current user and returns profile JSON
    echo json_encode([
        'success' => true,
        'profile' => $profile
    ]);
}

function handleDeleteProfile() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'POST required']);
        return;
    }
    requireCsrfToken();
    $user = requireAuth();

    $input = json_decode(file_get_contents('php://input'), true);
    $password = $input['password'] ?? '';

    if ($password === '') {
        echo json_encode(['success' => false, 'error' => 'Password confirmation is required']);
        return;
    }
    if (!password_verify($password, (string)($user['password_hash'] ?? ''))) {
        echo json_encode(['success' => false, 'error' => 'Password confirmation failed']);
        return;
    }

    $db = getDB();
    try {
        $db->beginTransaction();

        $stmt = $db->prepare("DELETE FROM user_sessions WHERE user_id = ?");
        $stmt->execute([$user['id']]);

        $stmt = $db->prepare("DELETE FROM login_attempts WHERE identifier IN (?, ?)");
        $stmt->execute([strtolower((string)$user['username']), strtolower((string)$user['email'])]);

        $stmt = $db->prepare("DELETE FROM users WHERE id = ?");
        $stmt->execute([$user['id']]);

        $db->commit();
    } catch (Throwable $e) {
        if ($db->inTransaction()) {
            $db->rollBack();
        }
        error_log('Delete profile failed: ' . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to delete profile']);
        return;
    }

    startSecureSession();
    session_unset();
    session_destroy();

    echo json_encode([
        'success' => true,
        'message' => 'Profile deleted successfully'
    ]);
}

function handleGetCsrf() {
    if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
        echo json_encode(['success' => false, 'error' => 'GET required']);
        return;
    }
        //CSFR token endpoint
    $token = getCsrfToken();
    echo json_encode([
        'success' => true,
        'csrf_token' => $token
    ]);
}

function hasUsersIsAdminColumn(PDO $db): bool {
    static $cached = null;
    if ($cached !== null) {
        return $cached;
    }
    try {
        $stmt = $db->prepare("
            SELECT COUNT(*) AS cnt
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'is_admin'
        ");
        $stmt->execute();
        $cached = intval($stmt->fetch()['cnt'] ?? 0) > 0;
    } catch (Throwable $e) {
        $cached = false;
    }
    return $cached;
}

function getUserPublicPayload(array $user): array {
    $email = strtolower((string)($user['email'] ?? ''));
    return [
        'id' => $user['id'],
        'uuid' => $user['uuid'],
        'username' => $user['username'],
        'email' => $user['email'],
        'full_name' => $user['full_name'],
        'is_admin' => !empty($user['is_admin']) || (ADMIN_EMAIL !== '' && $email === ADMIN_EMAIL)
    ];
}
?>
