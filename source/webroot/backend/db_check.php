<?php
require_once 'config.php'; //needs config.php  so it loads app_env, getDB, getPythonCommand

header('Content-Type: application/json'); //response type JSON
//allows endpoint when app_env = local
if (APP_ENV !== 'local') {
    http_response_code(403); //if not local return 403 error
    echo json_encode([
        'success' => false,
        'error' => 'Forbidden outside local environment'
    ]);
    exit;
}

$dbOk = false;
$dbError = null;
try {
    $db = getDB();
    $db->query('SELECT 1');
    $dbOk = true;
} catch (Throwable $e) {
    $dbError = $e->getMessage();
}

$pythonBin = getPythonCommand();
$pythonOutput = [];
$pythonExitCode = 0;
exec($pythonBin . ' --version 2>&1', $pythonOutput, $pythonExitCode);

//final JSON payload diagnostic whcih returns full diagnostic object
echo json_encode([
    'success' => true,
    'app_env' => APP_ENV,
    'pdo_mysql_loaded' => extension_loaded('pdo_mysql'),
    'pdo_drivers' => class_exists('PDO') ? PDO::getAvailableDrivers() : [],
    'loaded_ini' => php_ini_loaded_file(),
    'extension_dir' => ini_get('extension_dir'),
    'db_connected' => $dbOk,
    'db_error' => $dbError,
    'python_bin' => $pythonBin,
    'python_ok' => $pythonExitCode === 0,
    'python_version' => implode("\n", $pythonOutput)
]);
