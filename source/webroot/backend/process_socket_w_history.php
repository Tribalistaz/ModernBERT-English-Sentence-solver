<?php
/**
 * Socket-Based Backend with History Saving
 * Automatically saves predictions to database if user is logged in
 *
 * Purpose of this file:
 * Accept AJAX JSON requests from frontend.
 * Call Python socket client script to talk to long-running predictor server
 * Return normalized JSON response to frontend
 *Persist prediction history for authenticated users
 */

require_once 'config.php';

// This endpoint is API-only; always return JSON content type
header('Content-Type: application/json');
// Restrict cross-origin methods to what this endpoint needs
sendCorsHeaders(['POST', 'OPTIONS']);

// Enforce method contract early to keep endpoint behavior predictable
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    echo json_encode(['success' => false, 'error' => 'Only POST requests are allowed']);
    exit;
}
// POST endpoint for Cross-Site Request Forgery (CSRF) protection
requireCsrfToken();
// Increase execution limit for potentially slower model calls
applyBackendExecutionTimeout();

// Read raw JSON body and decode into associative array
$input = file_get_contents('php://input');
$data = json_decode($input, true);

// validate payload field
if (!isset($data['sentence']) || empty(trim($data['sentence']))) {
    echo json_encode(['success' => false, 'error' => 'No sentence provided']);
    exit;
}

// normalizing user spaces. equals out double spaces for BERT.
$sentence = preg_replace('/\s+/u', ' ', trim($data['sentence']));
$mode = isset($data['mode']) ? $data['mode'] : 'best';

// Clamp to allowed values so downstream code receives valid options only
if (!in_array($mode, ['best', 'top3'], true)) {
    $mode = 'best';
}

// Resolve current user from session; endpoint still works for guests
$currentUser = getCurrentUser();

// Build runtime paths and python executable command from config
$pythonPath = getPythonCommand();
$clientPath = resolvePythonArtifact('PYTHON_CLIENT_PATH', 'predictor_client.py');

// fail if socket client script is missing, because inference cannot proceed
if (!file_exists($clientPath)) {
    echo json_encode(['success' => false, 'error' => 'Socket client not found']);
    exit;
}

// Build shell command:
// escapeshellarg protects sentence/mode/path values
// stderr behavior differs by environment:
$escapedSentence = escapeshellarg($sentence);
$escapedMode = escapeshellarg('--mode=' . $mode);
$stderrRedirect = (APP_ENV === 'local')
    ? '2>&1'
    : ((strtoupper(substr(PHP_OS, 0, 3)) === 'WIN') ? '2>nul' : '2>/dev/null');

$command = sprintf(
    '%s %s %s %s %s',
    $pythonPath,
    escapeshellarg($clientPath),
    $escapedSentence,
    $escapedMode,
    $stderrRedirect
);

// Measure end-to-end backend processing duration for history
$startTime = microtime(true);

// Execute Python socket client script; it returns JSON on stdout
$output = [];
$returnCode = 0;
exec($command, $output, $returnCode);

$processingTime = round((microtime(true) - $startTime) * 1000); // milliseconds

// Reassemble multiline stdout and decode as JSON
$outputString = implode("\n", $output);
$clientResponse = json_decode($outputString, true);

// If Python output is not valid JSON, return actionable hint to caller
if ($clientResponse === null) {
    echo json_encode([
        'success' => false,
        'error' => 'Failed to parse client response',
        'hint' => 'Make sure predictor_server.py is running!'
    ]);
    exit;
}

// Predictor client returned explicit failure; forward message as API error
if (!$clientResponse['success']) {
    echo json_encode([
        'success' => false,
        'error' => $clientResponse['error']
    ]);
    exit;
}

// Persist history only for authenticated users
// guest users can still use the app without login and cache history locally
// logged-in users get personal history timeline
if ($currentUser) {
    try {
        $db = getDB();
        // Store normalized request/response metadata for later retrieval/statistics
        $stmt = $db->prepare("
            INSERT INTO prediction_history 
            (user_id, input_sentence, reconstructed_sentence, predictions_json, 
             prediction_mode, prediction_type, processing_time_ms, success)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ");
        
        $predictionData = $clientResponse['data'];
        $stmt->execute([
            $currentUser['id'],
            $sentence,
            $predictionData['reconstructed_sentence'] ?? null,
            json_encode($predictionData),
            $mode,
            $predictionData['type'] ?? null,
            $processingTime,
            true
        ]);
        
        $historyId = $db->lastInsertId();
    } catch (PDOException $e) {
        // history persistence should not break core response path
        error_log("Failed to save history: " . $e->getMessage());
        // Return result anyway, but mark history as not saved
        $historyId = null;
    }
}

// Final API response accessed by frontend
// prediction data
// processing metrics
// history persistence flags
echo json_encode([
    'success' => true,
    'data' => $clientResponse['data'],
    'timestamp' => date('Y-m-d H:i:s'),
    'method' => 'socket',
    'processing_time_ms' => $processingTime,
    'model_name' => $clientResponse['data']['system']['model_name'] ?? ($clientResponse['data']['system']['word_prediction'] ?? 'Unknown model'),
    'mode' => $mode,
    'cache' => $clientResponse['cache'] ?? null,
    'saved_to_history' => isset($historyId) && $historyId > 0,
    'history_id' => $historyId ?? null
]);
?>
