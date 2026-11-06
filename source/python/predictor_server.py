"""
Sentence Rebuilder - Socket Server
Keeps the model loaded in memory and adds request-level response caching.
"""

import copy
import hashlib
import importlib
import json
import os
import socket
import sys
import threading
from collections import OrderedDict

# predictor module without hard-coded static imports
_predictor_module = None
for _module_name in ('backend.predictor', 'predictor'):
    try:
        _predictor_module = importlib.import_module(_module_name)
        break
    except ModuleNotFoundError:
        continue

if _predictor_module is None:
    raise ModuleNotFoundError("Could not import predictor module via 'backend.predictor' or 'predictor'")

load_dictionary = getattr(_predictor_module, 'load_dictionary')
process_sentence = getattr(_predictor_module, 'process_sentence')
initialize_predictor_runtime = getattr(_predictor_module, 'initialize_predictor_runtime', lambda: None)
MODEL_NAME = str(getattr(_predictor_module, 'MODEL_LABEL', 'unknown-model'))
MODEL_SOURCE = str(getattr(_predictor_module, 'MODEL_SOURCE', MODEL_NAME))
MAX_OUTPUT_PREDICTIONS = int(getattr(_predictor_module, 'MAX_OUTPUT_PREDICTIONS', 3))
MIXED_FAST_WORD_TOP_K = int(getattr(_predictor_module, 'MIXED_FAST_WORD_TOP_K', 10))
MIXED_FAST_WORD_SCAN_LIMIT = int(getattr(_predictor_module, 'MIXED_FAST_WORD_SCAN_LIMIT', 10))

# server config
HOST = '127.0.0.1'
PORT = 65432
DICTIONARY = None

APP_ENV = str(os.getenv('APP_ENV', 'local')).strip().lower()
DEBUG_FLAG = str(os.getenv('PREDICTOR_DEBUG', '')).strip().lower()
DEBUG = DEBUG_FLAG in {'1', 'true', 'yes', 'on'} if DEBUG_FLAG else APP_ENV != 'production'

# Request level response cache
#cache keyed by sentence/mode/model/dictionary-signature/runtime-flags with thread-safe lock and cache-hit annotations in response
CACHE_MAX_ITEMS = max(8, int(os.getenv('CACHE_SIZE', '128')))
_RESPONSE_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_DICTIONARY_OBJECTS = {}
_DICTIONARY_LOCK = threading.Lock()

# Runtime flags that can change ranking/output are part of the cache key
# prevents stale cache collisions when adjusting inference settings
RUNTIME_FLAGS_SIGNATURE = (
    f"max_output={MAX_OUTPUT_PREDICTIONS}",
    f"word_top_k={MIXED_FAST_WORD_TOP_K}",
    f"word_scan={MIXED_FAST_WORD_SCAN_LIMIT}",
)


def _log(message):
    print(message, file=sys.stderr)


def _debug(message):
    if DEBUG:
        _log(message)


def _normalize_sentence_for_cache(sentence):
    return str(sentence or '')

# helper function for dictionary
# Builds dictionary from path + metadata
# Dictionary changes accounted for with new cache key
def make_dictionary_signature(dict_path):
    """
    Build dictionary signature for cache key stability.
    If provided path is missing/unreadable, use DEFAULT.
    """
    path = str(dict_path or '').strip()
    if not path:
        return "DEFAULT" #default fallback signature in case non is provided
    try:
        if not os.path.isabs(path):
            path = os.path.normpath(os.path.join(os.path.dirname(__file__), path))
        if not os.path.exists(path):
            return "DEFAULT"
        stat = os.stat(path)
        return (path, int(stat.st_mtime), int(stat.st_size))
    except Exception:
        return "DEFAULT"


def _dictionary_path_from_signature(dict_sig):
    if isinstance(dict_sig, tuple) and len(dict_sig) >= 1:
        return str(dict_sig[0])
    return None

# Support optional per-request dictionary path without breaking protocol
# loaded dictionaries are by signature in memory
def _get_dictionary_for_signature(dict_sig):
    """
    Return a dictionary object for the provided signature.
    Uses default preloaded dictionary for DEFAULT signature.
    """
    if dict_sig == "DEFAULT":
        return DICTIONARY

    dict_path = _dictionary_path_from_signature(dict_sig)
    if not dict_path:
        return DICTIONARY

    with _DICTIONARY_LOCK:
        cached = _DICTIONARY_OBJECTS.get(dict_sig)
        if cached is not None:
            return cached
        loaded = load_dictionary(dict_path)
        _DICTIONARY_OBJECTS[dict_sig] = loaded
        return loaded

#cache key helper
#exact sentence text, mode slection, model id, dictionary signature, runtime flags signature are included in cache key
def make_cache_key(sentence, mode, model_id, dict_signature, runtime_flags=None):
    normalized_mode = mode if mode in {'top', 'top3'} else 'top'
    runtime_flags = tuple(runtime_flags or ())
    return (
        _normalize_sentence_for_cache(sentence),
        normalized_mode,
        str(model_id or ''),
        dict_signature,
        runtime_flags,
    )

# Short stable hash used for logs only to avoid printing full key data
def _cache_key_hash(cache_key):
    key_json = json.dumps(cache_key, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(key_json.encode('utf-8')).hexdigest()[:8]


def _build_cache_key(sentence, mode, dict_signature):
    normalized_mode = mode if mode in {'top', 'top3'} else 'top'
    model_id = f"{MODEL_NAME}|{MODEL_SOURCE}"
    return make_cache_key(
        sentence=sentence,
        mode=normalized_mode,
        model_id=model_id,
        dict_signature=dict_signature,
        runtime_flags=RUNTIME_FLAGS_SIGNATURE,
    )


def _get_cached_response(cache_key):
    with _CACHE_LOCK:
        cached = _RESPONSE_CACHE.get(cache_key)
        if cached is None:
            return None
        _RESPONSE_CACHE.move_to_end(cache_key)
        return copy.deepcopy(cached)


def _set_cached_response(cache_key, response):
    with _CACHE_LOCK:
        _RESPONSE_CACHE[cache_key] = copy.deepcopy(response)
        _RESPONSE_CACHE.move_to_end(cache_key)
        while len(_RESPONSE_CACHE) > CACHE_MAX_ITEMS:
            _RESPONSE_CACHE.popitem(last=False)

#loads dictionary 
def load_server_dictionary():
    """Load dictionary once at server startup"""
    global DICTIONARY
    dict_path = os.path.join(os.path.dirname(__file__), 'english_dictionary.txt')
    DICTIONARY = load_dictionary(dict_path)
    _log(f"Dictionary loaded: {len(DICTIONARY):,} words")


def handle_client(conn, addr):
    """Handle individual client connection"""
    _debug(f"Connected by {addr}")

    try:
        data = b''
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
            try:
                json.loads(data.decode('utf-8'))
                break
            except json.JSONDecodeError:
                continue

        if not data:
            _debug("No data received")
            return

        request = json.loads(data.decode('utf-8'))
        sentence = str(request.get('sentence', '')).strip()
        mode = str(request.get('mode', 'top')).strip().lower()
        dict_path = request.get('dictionary_path')
        if dict_path is None:
            dict_path = request.get('dict_path')
        if mode == 'best':
            mode = 'top'
        if mode not in {'top', 'top3'}:
            mode = 'top'

        dict_signature = make_dictionary_signature(dict_path)
        cache_key = _build_cache_key(sentence, mode, dict_signature)
        cache_hash = _cache_key_hash(cache_key)
        request_dictionary = _get_dictionary_for_signature(dict_signature)

        _debug(f"Request: '{sentence}' (mode: {mode}, dict_sig: {dict_signature})")
#repeated identical request return fas retrival from memory with cache bound kept
        if not sentence:
            response = {
                'success': False,
                'error': 'No sentence provided'
            }
        else:
            cached_response = _get_cached_response(cache_key) #hit/miss behaviour implementation
            if cached_response is not None:
                response = cached_response
                response['cache'] = {'hit': True}
                _log(f"CACHE HIT {cache_hash}")
            else:
                result = process_sentence(sentence, request_dictionary, mode)
                response = {
                    'success': True,
                    'data': result
                }
                _set_cached_response(cache_key, response)
                response['cache'] = {'hit': False}
                _log(f"CACHE MISS {cache_hash}")

        response_json = json.dumps(response).encode('utf-8')
        conn.sendall(response_json)
        _debug(f"Response sent ({len(response_json)} bytes)")

    except Exception as e:
        _log(f"Error handling client: {e}")
        error_response = {
            'success': False,
            'error': str(e)
        }
        try:
            conn.sendall(json.dumps(error_response).encode('utf-8'))
        except Exception:
            pass

    finally:
        conn.close()
        _debug(f"Connection closed: {addr}")

#server start
def start_server():
    """Start the socket server"""
    _log('=' * 60)
    _log('Sentence Rebuilder Socket Server')
    _log('=' * 60)
    _log('Initializing tokenizer/model runtime...')
    initialize_predictor_runtime()
    _log('Loading dictionary...')
    load_server_dictionary()
    _log('Tokenizer/model loaded and ready')
    _log(f'Cache config: max_items={CACHE_MAX_ITEMS}')
    _log('')

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((HOST, PORT))
        server_socket.listen()

        _log('=' * 60)
        _log(f'Server listening on {HOST}:{PORT}')
        _log('=' * 60)
        _log('Ready to accept connections!')
        _log('Press Ctrl+C to stop the server')
        _log('')

        try:
            while True:
                conn, addr = server_socket.accept()
                client_thread = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
                client_thread.start()
        except KeyboardInterrupt:
            _log('')
            _log('Server shutting down...')
            _log('Goodbye!')


if __name__ == '__main__':
    start_server()
