<?php
/**
 * Sentence Rebuilder - Socket-Based Backend
 * Uses socket client to connect to running Python server
 * MUCH FASTER - no model reloading!
 */

require_once 'config.php';

header('Content-Type: application/json');
sendCorsHeaders(['POST', 'OPTIONS']);

// Only accept POST requests
if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
    echo json_encode([
        'success' => false,
        'error' => 'Only POST requests are allowed'
    ]);
    exit;
}
requireCsrfToken();
applyBackendExecutionTimeout();

// get JSON input
$input = file_get_contents('php://input');
$data = json_decode($input, true);

// validate input
if (!isset($data['sentence']) || empty(trim($data['sentence']))) {
    echo json_encode([
        'success' => false,
        'error' => 'No sentence provided'
    ]);
    exit;
}

// normalizes double spaces from user input to aid BERT processing
$sentence = preg_replace('/\s+/u', ' ', trim($data['sentence']));
$mode = isset($data['mode']) ? $data['mode'] : 'best';

// validation mode
if (!in_array($mode, ['best', 'top3'], true)) {
    $mode = 'best';
}

// Paths
$pythonPath = getPythonCommand();
$clientPath = resolvePythonArtifact('PYTHON_CLIENT_PATH', 'predictor_client.py');

// Check if client script exists
if (!file_exists($clientPath)) {
    echo json_encode([
        'success' => false,
        'error' => 'Socket client not found at: ' . $clientPath
    ]);
    exit;
}

// Build command
$escapedSentence = escapeshellarg($sentence);
$escapedMode = escapeshellarg('--mode=' . $mode);

// in local mode keep stderr in output for debugging
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

// Log command for debugging
error_log("Executing socket client: " . $command);

// Execute client script
$output = [];
$returnCode = 0;
exec($command, $output, $returnCode);

// Join output lines
$outputString = implode("\n", $output);

// parse JSON response
$clientResponse = json_decode($outputString, true);

if ($clientResponse === null) {
    // JSON parsing failed
    echo json_encode([
        'success' => false,
        'error' => 'Failed to parse client response',
        'raw_output' => $outputString,
        'return_code' => $returnCode,
        'hint' => 'Make sure predictor_server.py is running!'
    ]);
    exit;
}

// Check if client reported an error
if (!$clientResponse['success']) {
    echo json_encode([
        'success' => false,
        'error' => $clientResponse['error'],
        'hint' => 'Is predictor_server.py running? Start it with: ' . $pythonPath . ' predictor_server.py'
    ]);
    exit;
}

// Success - return client results
echo json_encode([
    'success' => true,
    'data' => $clientResponse['data'],
    'timestamp' => date('Y-m-d H:i:s'),
    'method' => 'socket'  // Indicates this used the fast socket method
]);
?>



