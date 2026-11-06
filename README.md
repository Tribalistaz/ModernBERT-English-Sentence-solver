
# Sentence Rebuilder (Capstone)

Sentence Rebuilder is a browser-based NLP system that reconstructs incomplete English sentences using a Python language model service and a PHP backend.

This repository is currently in active development for an undergraduate capstone project.

## Project Goals

- Provide an interactive web app (HTML/CSS/JavaScript) for sentence reconstruction.
- Use AJAX + JSON for frontend/backend communication (no full page refresh).
- Use PHP as backend orchestration layer.
- Run prediction logic in Python.
- Support user accounts and history storage (MariaDB).
- Compare multiple transformer models (ongoing evaluation).

## Current Architecture

Frontend (Browser):
- `source/webroot/frontend/index.html`
- Sends requests to backend endpoints with AJAX/JSON.

Backend (PHP):
- `source/webroot/backend/process.php` (direct Python execution mode)
- `source/webroot/backend/process_socket.php` (socket client mode)
- `source/webroot/backend/process_socket_w_history.php` (socket + DB history)
- `source/webroot/backend/auth.php` (register/login/logout/session check)
- `source/webroot/backend/history.php` (user prediction history)

Python Service:
- `source/python/predictor.py` (core prediction pipeline)
- `source/python/predictor_server.py` (long-running socket server)
- `source/python/predictor_client.py` (client script called from PHP)

Database:
- MariaDB schema in `database/database.sql`
- Includes users, sessions, preferences, and prediction history.

## Tech Stack

- PHP 8.x
- Python 3.10.x
- HTML/CSS/JavaScript
- MariaDB
- Transformers + PyTorch

## Environment Configuration

Credentials and runtime settings are configured via:
- `source/webroot/.env` (active for new Bitbucket-style layout)
- `.env` (legacy local root config, optional fallback)

Use `source/webroot/.env.example` as template:

```env
APP_ENV=production
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=sentence_rebuilder
DB_USER=change_me
DB_PASS=change_me
PYTHON_BIN=python3
PYTHON_CLIENT_PATH=../python/predictor_client.py
PYTHON_SCRIPT_PATH=../python/predictor.py
PYTHON_DICT_PATH=../python/english_dictionary.txt
SESSION_COOKIE_SECURE=1
```

## Quick Start (Local)

1. Create `source/webroot/.env` from `source/webroot/.env.example` and set your local values.
2. Import database schema from `database/database.sql`.
3. Ensure Python dependencies are installed (`transformers`, `torch`, etc.).
4. Start services with:

```bat
Start_servers.bat
```

Alternative (same behavior):

```bat
Start_servers_bitbucket.bat
```

5. Open:

```text
http://127.0.0.1:8080/frontend/index.html
```

## Status

Implemented:
- Browser client + AJAX JSON flow.
- PHP backend to Python service integration.
- MariaDB integration with user/session/history schema.
- Configurable runtime via `.env`.
- Local diagnostics endpoint (`source/webroot/backend/db_check.php` in local mode).

## Academic Note

This is an educational capstone project. Design decisions prioritize explainability, iterative experimentation, and practical deployment learning.
=======
# Word-Guesser


English Word Guesser and word completion AI using BERT AI and English Dictionary.
Backend: Python, PHP, PY Torch
Frontend: HTML
Database: SQL with myphpadmin fully functional login
AI: BERT AI for English

PHP Socket Server running for fast load times, cache, user login and full SQL database. 

Aiming for very high accuracy in guessing words and completing missing letters correctly.
>>>>>>> 7165a4cf9193dd4cdcd1cea175b88b221388ec64
