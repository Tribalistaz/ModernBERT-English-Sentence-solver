<?php
/**
 * Prediction History API
 * Handles saving and retrieving prediction history
 */

require_once 'config.php'; //needs config.php

header('Content-Type: application/json');
sendCorsHeaders(['GET', 'POST', 'DELETE', 'OPTIONS']);

$method = $_SERVER['REQUEST_METHOD'];
$action = $_GET['action'] ?? '';
//action dispatcher 
switch ($action) {
    case 'save':
        handleSave();
        break;
    case 'list':
        handleList();
        break;
    case 'get':
        handleGet();
        break;
    case 'delete':
        handleDelete();
        break;
    case 'clear':
        handleClear();
        break;
    case 'stats':
        handleStats();
        break;
    case 'metrics':
        handleMetrics();
        break;
    default:
        echo json_encode(['success' => false, 'error' => 'Invalid action']);
}

//predictions will be added to user history
function handleSave() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') { //Post enforcer
        echo json_encode(['success' => false, 'error' => 'POST required']);
        return;
    }
    requireCsrfToken();
    
    $input = json_decode(file_get_contents('php://input'), true);
    
    $user = getCurrentUser();
    $userId = $user ? $user['id'] : null;
    
    $inputSentence = $input['input_sentence'] ?? '';
    $reconstructed = $input['reconstructed_sentence'] ?? null;
    $predictions = $input['predictions'] ?? null;
    $mode = $input['mode'] ?? 'best';
    $type = $input['type'] ?? null;
    $processingTime = $input['processing_time'] ?? null;
    $success = $input['success'] ?? true;
    $errorMessage = $input['error_message'] ?? null;
    
    if (empty($inputSentence)) {
        echo json_encode(['success' => false, 'error' => 'Input sentence required']);
        return;
    }
    
    $db = getDB(); //open database
    //prediction history 
    try {
        $stmt = $db->prepare("
            INSERT INTO prediction_history 
            (user_id, input_sentence, reconstructed_sentence, predictions_json, 
             prediction_mode, prediction_type, processing_time_ms, success, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ");
        
        $stmt->execute([
            $userId,
            $inputSentence,
            $reconstructed,
            json_encode($predictions), //stores predicted text in JSON format
            $mode,
            $type,
            $processingTime,
            $success,
            $errorMessage
        ]);
        
        $historyId = $db->lastInsertId();
        
        echo json_encode([
            'success' => true,
            'message' => 'History saved',
            'history_id' => $historyId
        ]);
    } catch (PDOException $e) {
        error_log("History save error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to save history']);
    }
}

//fetch user's prediction history
function handleList() {
    $user = getCurrentUser();
    
    if (!$user) {
        echo json_encode(['success' => false, 'error' => 'Authentication required']);
        return;
    }
    
    $limit = isset($_GET['limit']) ? intval($_GET['limit']) : 50;
    $offset = isset($_GET['offset']) ? intval($_GET['offset']) : 0;
    
    $db = getDB(); //open database
    
    try {
        // Get total count
        $stmt = $db->prepare("SELECT COUNT(*) as total FROM prediction_history WHERE user_id = ?");
        $stmt->execute([$user['id']]);
        $total = $stmt->fetch()['total'];
        
        // Get history
        $stmt = $db->prepare("
            SELECT 
                id,
                input_sentence,
                reconstructed_sentence,
                prediction_mode,
                prediction_type,
                processing_time_ms,
                success,
                created_at
            FROM prediction_history
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        ");
        $stmt->execute([$user['id'], $limit, $offset]);
        $history = $stmt->fetchAll();
        
        echo json_encode([
            'success' => true,
            'history' => $history,
            'total' => $total,
            'limit' => $limit,
            'offset' => $offset
        ]);
    } catch (PDOException $e) {
        error_log("History list error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to fetch history']);
    }
}

//prediction details
function handleGet() {
    $user = getCurrentUser();
    
    if (!$user) {
        echo json_encode(['success' => false, 'error' => 'Authentication required']);
        return;
    }
    
    $id = isset($_GET['id']) ? intval($_GET['id']) : 0;
    
    if ($id <= 0) {
        echo json_encode(['success' => false, 'error' => 'Invalid ID']);
        return;
    }
    
    $db = getDB();
    
    try {
        $stmt = $db->prepare("
            SELECT * FROM prediction_history 
            WHERE id = ? AND user_id = ?
        ");
        $stmt->execute([$id, $user['id']]);
        $prediction = $stmt->fetch();
        
        if (!$prediction) {
            echo json_encode(['success' => false, 'error' => 'Prediction not found']);
            return;
        }
        
        // Decode JSON
        $prediction['predictions_json'] = json_decode($prediction['predictions_json'], true);
        
        echo json_encode([
            'success' => true,
            'prediction' => $prediction
        ]);
    } catch (PDOException $e) {
        error_log("History get error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to fetch prediction']);
    }
}

//function for user to delete predictions from history
function handleDelete() {
    if ($_SERVER['REQUEST_METHOD'] !== 'DELETE' && $_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'DELETE or POST required']);
        return;
    }
    requireCsrfToken();
    
    $user = getCurrentUser();
    
    if (!$user) {
        echo json_encode(['success' => false, 'error' => 'Authentication required']);
        return;
    }
    
    $id = isset($_GET['id']) ? intval($_GET['id']) : 0;
    
    if ($id <= 0) {
        echo json_encode(['success' => false, 'error' => 'Invalid ID']);
        return;
    }
    
    $db = getDB();
    
    try {
        $stmt = $db->prepare("DELETE FROM prediction_history WHERE id = ? AND user_id = ?");
        $stmt->execute([$id, $user['id']]);
        
        if ($stmt->rowCount() > 0) {
            echo json_encode([
                'success' => true,
                'message' => 'Prediction deleted'
            ]);
        } else {
            echo json_encode(['success' => false, 'error' => 'Prediction not found']);
        }
    } catch (PDOException $e) {
        error_log("History delete error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to delete prediction']);
    }
}

// clear all history for current user
function handleClear() {
    if ($_SERVER['REQUEST_METHOD'] !== 'DELETE' && $_SERVER['REQUEST_METHOD'] !== 'POST') {
        echo json_encode(['success' => false, 'error' => 'DELETE or POST required']);
        return;
    }
    requireCsrfToken();
    
    $user = getCurrentUser();
    
    if (!$user) {
        echo json_encode(['success' => false, 'error' => 'Authentication required']);
        return;
    }
    
    $db = getDB();
    
    try {
        $stmt = $db->prepare("DELETE FROM prediction_history WHERE user_id = ?");
        $stmt->execute([$user['id']]);
        
        echo json_encode([
            'success' => true,
            'message' => 'History cleared'
        ]);
    } catch (PDOException $e) {
        error_log("History clear error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to clear history']);
    }
}

//user statistics fetched
function handleStats() {
    $user = getCurrentUser();
    
    if (!$user) {
        echo json_encode(['success' => false, 'error' => 'Authentication required']);
        return;
    }
    
    $db = getDB();
    
    try {
        // Get overall stats
        $stmt = $db->prepare("
            SELECT 
                COUNT(*) as total_predictions,
                COUNT(CASE WHEN success = TRUE THEN 1 END) as successful_predictions,
                AVG(processing_time_ms) as avg_processing_time,
                MIN(created_at) as first_prediction,
                MAX(created_at) as last_prediction
            FROM prediction_history
            WHERE user_id = ?
        ");
        $stmt->execute([$user['id']]);
        $stats = $stmt->fetch();
        
        // Get predictions by type
        $stmt = $db->prepare("
            SELECT 
                prediction_type,
                COUNT(*) as count
            FROM prediction_history
            WHERE user_id = ?
            GROUP BY prediction_type
        ");
        $stmt->execute([$user['id']]);
        $byType = $stmt->fetchAll();
        
        // Get predictions by mode
        $stmt = $db->prepare("
            SELECT 
                prediction_mode,
                COUNT(*) as count
            FROM prediction_history
            WHERE user_id = ?
            GROUP BY prediction_mode
        ");
        $stmt->execute([$user['id']]);
        $byMode = $stmt->fetchAll();
        
        // Get recent activity (last 7 days)
        $stmt = $db->prepare("
            SELECT 
                DATE(created_at) as date,
                COUNT(*) as count
            FROM prediction_history
            WHERE user_id = ? AND created_at >= DATE_SUB(NOW(), INTERVAL 7 DAY)
            GROUP BY DATE(created_at)
            ORDER BY date DESC
        ");
        $stmt->execute([$user['id']]);
        $recentActivity = $stmt->fetchAll();
        
        echo json_encode([
            'success' => true,
            'stats' => $stats,
            'by_type' => $byType,
            'by_mode' => $byMode,
            'recent_activity' => $recentActivity
        ]);
    } catch (PDOException $e) {
        error_log("Stats error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to fetch statistics']);
    }
}

// Model/performance metrics for comparison runs (ModernBERT vs alternatives)
function handleMetrics() {
    $user = getCurrentUser();

    if (!$user) {
        echo json_encode(['success' => false, 'error' => 'Authentication required']);
        return;
    }

    $db = getDB();

    try {
        $stmt = $db->prepare("
            SELECT
                id,
                input_sentence,
                prediction_mode,
                prediction_type,
                processing_time_ms,
                predictions_json,
                created_at
            FROM prediction_history
            WHERE user_id = ? AND success = 1
            ORDER BY created_at DESC
            LIMIT 1000
        ");
        $stmt->execute([$user['id']]);
        $rows = $stmt->fetchAll();

        $overall = [
            'sample_size' => 0,
            'avg_processing_time_ms' => 0,
            'min_processing_time_ms' => null,
            'max_processing_time_ms' => null
        ];
        $modelBuckets = [];
        $recentSamples = [];

        foreach ($rows as $row) {
            $procMs = is_numeric($row['processing_time_ms']) ? intval($row['processing_time_ms']) : null;
            if ($procMs === null) {
                continue;
            }

            $parsed = json_decode((string)$row['predictions_json'], true);
            $modelName = 'Unknown model';
            if (is_array($parsed)) {
                if (!empty($parsed['system']['model_name'])) {
                    $modelName = (string)$parsed['system']['model_name'];
                } elseif (!empty($parsed['system']['word_prediction'])) {
                    $modelName = (string)$parsed['system']['word_prediction'];
                }
            }

            if (!isset($modelBuckets[$modelName])) {
                $modelBuckets[$modelName] = [
                    'model_name' => $modelName,
                    'sample_size' => 0,
                    'sum_processing_time_ms' => 0,
                    'min_processing_time_ms' => null,
                    'max_processing_time_ms' => null
                ];
            }

            $overall['sample_size'] += 1;
            $overall['avg_processing_time_ms'] += $procMs;
            $overall['min_processing_time_ms'] = $overall['min_processing_time_ms'] === null
                ? $procMs
                : min($overall['min_processing_time_ms'], $procMs);
            $overall['max_processing_time_ms'] = $overall['max_processing_time_ms'] === null
                ? $procMs
                : max($overall['max_processing_time_ms'], $procMs);

            $modelBuckets[$modelName]['sample_size'] += 1;
            $modelBuckets[$modelName]['sum_processing_time_ms'] += $procMs;
            $modelBuckets[$modelName]['min_processing_time_ms'] = $modelBuckets[$modelName]['min_processing_time_ms'] === null
                ? $procMs
                : min($modelBuckets[$modelName]['min_processing_time_ms'], $procMs);
            $modelBuckets[$modelName]['max_processing_time_ms'] = $modelBuckets[$modelName]['max_processing_time_ms'] === null
                ? $procMs
                : max($modelBuckets[$modelName]['max_processing_time_ms'], $procMs);

            if (count($recentSamples) < 20) {
                $recentSamples[] = [
                    'history_id' => intval($row['id']),
                    'model_name' => $modelName,
                    'prediction_mode' => $row['prediction_mode'],
                    'prediction_type' => $row['prediction_type'],
                    'processing_time_ms' => $procMs,
                    'input_sentence' => $row['input_sentence'],
                    'created_at' => $row['created_at']
                ];
            }
        }

        if ($overall['sample_size'] > 0) {
            $overall['avg_processing_time_ms'] = round($overall['avg_processing_time_ms'] / $overall['sample_size'], 2);
        }

        $perModel = array_values(array_map(function ($bucket) {
            $sampleSize = $bucket['sample_size'];
            $bucket['avg_processing_time_ms'] = $sampleSize > 0
                ? round($bucket['sum_processing_time_ms'] / $sampleSize, 2)
                : 0;
            unset($bucket['sum_processing_time_ms']);
            return $bucket;
        }, $modelBuckets));

        usort($perModel, function ($a, $b) {
            return $b['sample_size'] <=> $a['sample_size'];
        });

        echo json_encode([
            'success' => true,
            'overall' => $overall,
            'per_model' => $perModel,
            'recent_samples' => $recentSamples
        ]);
    } catch (Throwable $e) {
        error_log("Metrics error: " . $e->getMessage());
        echo json_encode(['success' => false, 'error' => 'Failed to fetch metrics']);
    }
}
?>

