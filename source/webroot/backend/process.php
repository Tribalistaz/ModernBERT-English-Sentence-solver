<?php
/**
 * Sentence Rebuilder - Backend PHP Handler
 * Receives AJAX requests, executes Python script, returns JSON
 */

require_once 'config.php';

// Set headers for JSON response and CORS
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

// Get JSON input
$input = file_get_contents('php://input');
$data = json_decode($input, true);

// Validate input
if (!isset($data['sentence']) || empty(trim($data['sentence']))) {
    echo json_encode([
        'success' => false,
        'error' => 'No sentence provided'
    ]);
    exit;
}

// normalize user spaces. equals out double spaces and user mistakes
$sentence = preg_replace('/\s+/u', ' ', trim($data['sentence']));
$mode = isset($data['mode']) ? $data['mode'] : 'best';

// Validate mode
if (!in_array($mode, ['best', 'top3'], true)) {
    $mode = 'best';
}

// path
$pythonPath = getPythonCommand();
$scriptPath = resolvePythonArtifact('PYTHON_SCRIPT_PATH', 'predictor.py');
$dictPath = resolvePythonArtifact('PYTHON_DICT_PATH', 'english_dictionary.txt');

// Check if files exist
if (!file_exists($scriptPath)) {
    echo json_encode([
        'success' => false,
        'error' => 'Python script not found at: ' . $scriptPath
    ]);
    exit;
}

// Build command - escape arguments for security
$escapedSentence = escapeshellarg($sentence);
$escapedDict = escapeshellarg($dictPath);
$escapedMode = escapeshellarg('--mode=' . $mode);

// in local keep stderr in output for debuggin
$stderrRedirect = (APP_ENV === 'local')
    ? '2>&1'
    : ((strtoupper(substr(PHP_OS, 0, 3)) === 'WIN') ? '2>nul' : '2>/dev/null');

$command = sprintf(
    '%s %s %s %s %s %s',
    $pythonPath,
    escapeshellarg($scriptPath),
    $escapedSentence,
    $escapedDict,
    $escapedMode,
    $stderrRedirect
);

// Log the command for debugging
error_log("Executing: " . $command);

// Execute Python script
$output = [];
$returnCode = 0;
exec($command, $output, $returnCode);

// Join output lines
$outputString = implode("\n", $output);

// parse JSON response from Python
$pythonResult = json_decode($outputString, true);

if ($pythonResult === null) {
    // JSON parsing failed
    echo json_encode([
        'success' => false,
        'error' => 'Failed to parse Python output',
        'raw_output' => $outputString,
        'return_code' => $returnCode
    ]);
    exit;
}

// Success - return Python results
echo json_encode([
    'success' => true,
    'data' => $pythonResult,
    'timestamp' => date('Y-m-d H:i:s')
]);
?>



