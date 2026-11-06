<?php
require_once 'config.php';

if (APP_ENV !== 'local') {
    http_response_code(403);
    echo 'Forbidden';
    exit;
}

phpinfo();
