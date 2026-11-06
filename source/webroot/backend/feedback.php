<?php
require_once 'config.php';
// CSFR required, POST-only, JSON implemented for feedback loop 
header('Content-Type: application/json');
sendCorsHeaders(['POST', 'OPTIONS']);

if (($_SERVER['REQUEST_METHOD'] ?? 'GET') !== 'POST') {
    echo json_encode(['success' => false, 'error' => 'Only POST requests are allowed']);
    exit;
}

requireCsrfToken();

$input = json_decode(file_get_contents('php://input'), true);
if (!is_array($input)) {
    echo json_encode(['success' => false, 'error' => 'Invalid JSON payload']);
    exit;
}
//category validation logic so user can submit errors under different categories
$allowedCategories = ['gender', 'words', 'regions', 'names', 'tense', 'other'];
$categoriesRaw = $input['categories'] ?? [];

if (is_string($categoriesRaw)) {
    $categoriesRaw = [$categoriesRaw];
}

if (!is_array($categoriesRaw)) {
    echo json_encode(['success' => false, 'error' => 'Invalid categories']);
    exit;
}

$categories = [];
foreach ($categoriesRaw as $cat) {
    $value = strtolower(trim((string)$cat));
    if ($value !== '' && in_array($value, $allowedCategories, true)) {
        $categories[] = $value;
    }
}
$categories = array_values(array_unique($categories));

if (count($categories) === 0) {
    echo json_encode(['success' => false, 'error' => 'Please select at least one error category']);
    exit;
}
//database insertion logic
$historyId = null;
if (isset($input['history_id']) && is_numeric($input['history_id'])) {
    $historyId = intval($input['history_id']);
    if ($historyId <= 0) {
        $historyId = null;
    }
}
//lengthguard caps for submission
$inputSentence = trim((string)($input['input_sentence'] ?? ''));
$predictedSentence = trim((string)($input['predicted_sentence'] ?? ''));
$correctedSentence = trim((string)($input['corrected_sentence'] ?? ''));
$notes = trim((string)($input['notes'] ?? ''));

if (function_exists('mb_strlen') && function_exists('mb_substr')) {
    if (mb_strlen($inputSentence) > 2700) {
        $inputSentence = mb_substr($inputSentence, 0, 2700);
    }
    if (mb_strlen($predictedSentence) > 2700) {
        $predictedSentence = mb_substr($predictedSentence, 0, 2700);
    }
    if (mb_strlen($correctedSentence) > 2700) {
        $correctedSentence = mb_substr($correctedSentence, 0, 2700);
    }
    if (mb_strlen($notes) > 700) {
        $notes = mb_substr($notes, 0, 700);
    }
} else {
    if (strlen($inputSentence) > 2700) {
        $inputSentence = substr($inputSentence, 0, 2700);
    }
    if (strlen($predictedSentence) > 2700) {
        $predictedSentence = substr($predictedSentence, 0, 2700);
    }
    if (strlen($correctedSentence) > 2700) {
        $correctedSentence = substr($correctedSentence, 0, 2700);
    }
    if (strlen($notes) > 700) {
        $notes = substr($notes, 0, 700);
    }
}

$user = getCurrentUser();
$userId = $user ? intval($user['id']) : null;

$db = getDB();
//database insertion logic ,fectches prediction id
if ($historyId !== null) {
    $stmt = $db->prepare("
        SELECT id, user_id, input_sentence, reconstructed_sentence
        FROM prediction_history
        WHERE id = ?
        LIMIT 1
    ");
    $stmt->execute([$historyId]);
    $historyRow = $stmt->fetch();

    if (!$historyRow) {
        $historyId = null;
    } else {
        if ($userId !== null && $historyRow['user_id'] !== null && intval($historyRow['user_id']) !== $userId) {
            echo json_encode(['success' => false, 'error' => 'Unauthorized history reference']);
            exit;
        }
        if ($inputSentence === '') {
            $inputSentence = (string)($historyRow['input_sentence'] ?? '');
        }
        if ($predictedSentence === '') {
            $predictedSentence = (string)($historyRow['reconstructed_sentence'] ?? '');
        }
    }
}

if ($inputSentence === '') {
    echo json_encode(['success' => false, 'error' => 'Input sentence is required']);
    exit;
}
//no empty submission, must contain meaningful data
if ($predictedSentence === '' && $correctedSentence === '' && $notes === '') {
    echo json_encode(['success' => false, 'error' => 'Please provide corrected text or feedback notes']);
    exit;
}
//predict feedback 
try {
    $stmt = $db->prepare("
        INSERT INTO prediction_feedback
        (history_id, user_id, input_sentence, predicted_sentence, corrected_sentence, categories_json, notes, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'new')
    ");
    $stmt->execute([
        $historyId,
        $userId,
        $inputSentence,
        $predictedSentence === '' ? null : $predictedSentence,
        $correctedSentence === '' ? null : $correctedSentence,
        json_encode($categories),
        $notes === '' ? null : $notes
    ]);
//feedback json return
    echo json_encode([
        'success' => true,
        'message' => 'Feedback submitted successfully',
        'feedback_id' => intval($db->lastInsertId())
    ]);
} catch (Throwable $e) {
    error_log('Feedback submit failed: ' . $e->getMessage());
    echo json_encode([
        'success' => false,
        'error' => 'Failed to submit feedback'
    ]);
}
?>
