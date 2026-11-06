<?php
// Route root domain requests to the frontend app entry point.
header('Location: /frontend/index.html', true, 302);
exit;

