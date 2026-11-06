"""
Sentence Rebuilder
Combines BERT AI for word prediction with dictionary matching for letter completion
word frequency ranking
"""

#library imports
import sys
import json
import re
import os
import math
from collections import OrderedDict
from threading import Lock
from typing import Any

# Runtime threading defaults for server CPU 
# oversubscription on small servers.
CPU_COUNT = max(1, int(os.cpu_count() or 1))
DEFAULT_TORCH_THREADS = max(1, min(CPU_COUNT, 4))
os.environ.setdefault('OMP_NUM_THREADS', str(DEFAULT_TORCH_THREADS))
os.environ.setdefault('MKL_NUM_THREADS', str(DEFAULT_TORCH_THREADS))

import torch
from transformers import AutoModelForMaskedLM, AutoTokenizer
#Runtime config layer
APP_ENV = str(os.getenv('APP_ENV', 'local')).strip().lower()
DEBUG_ENV = str(os.getenv('PREDICTOR_DEBUG', '')).strip().lower()
DEBUG = DEBUG_ENV in {'1', 'true', 'yes', 'on'} if DEBUG_ENV else APP_ENV != 'production'
MAX_OUTPUT_PREDICTIONS = 3
TORCH_THREADS = max(1, int(os.getenv('PREDICTOR_TORCH_THREADS', str(DEFAULT_TORCH_THREADS)))) #CPU thread tuning for PyTorch interference
TORCH_INTEROP_THREADS = max(1, int(os.getenv('PREDICTOR_TORCH_INTEROP_THREADS', '1'))) #prevents threads on server and reduces latency
LETTER_PATTERN_CACHE_MAX = max(512, int(os.getenv('LETTER_PATTERN_CACHE_MAX', '5000'))) #in-memory cache for letter pattern candidate matches
LETTER_PATTERN_CACHE = OrderedDict()
LETTER_PATTERN_CACHE_LOCK = Lock()
DICTIONARY_INDEX_CACHE = {} #prebuilt dictionary storage
MIXED_HEAVY_SENTENCE_LEN = max(80, int(os.getenv('MIXED_HEAVY_SENTENCE_LEN', '180'))) # thresholds to detect heavy mixed requests
MIXED_HEAVY_TOTAL_GAPS = max(3, int(os.getenv('MIXED_HEAVY_TOTAL_GAPS', '8'))) # thresholds to detect heavy mixed requests
MIXED_FAST_WORD_TOP_K = max(MAX_OUTPUT_PREDICTIONS, min(20, int(os.getenv('MIXED_FAST_WORD_TOP_K', '10')))) #fast path knobs used for mixed requests
MIXED_FAST_WORD_SCAN_LIMIT = max(MAX_OUTPUT_PREDICTIONS, min(20, int(os.getenv('MIXED_FAST_WORD_SCAN_LIMIT', '10'))))
LETTER_FAST_RANK_POOL = max(50, int(os.getenv('LETTER_FAST_RANK_POOL', '250'))) #lower candidate expansion and smaller ranking pool to reduce latency


def debug_log(*args, **kwargs):
    if DEBUG:
        kwargs.setdefault('file', sys.stderr)
        print(*args, **kwargs)


def info_log(*args, **kwargs):
    kwargs.setdefault('file', sys.stderr)
    print(*args, **kwargs)


def configure_torch_threads():
    """Set Torch CPU thread counts once at startup."""
    try:
        torch.set_num_threads(TORCH_THREADS)
    except Exception as exc:
        debug_log(f"DEBUG: torch.set_num_threads failed: {exc}")

    try:
        torch.set_num_interop_threads(TORCH_INTEROP_THREADS)
    except Exception:
        # set_num_interop_threads can only be set once in some runtimes.
        pass


#starup log, only stderr because otherwise debug output would invalidate JSON output
info_log("Initializing Sentence Rebuilder...")
configure_torch_threads()

# Load masked-language model (ModernBERT-base by default)
MODEL_ID = 'answerdotai/modernbert-base'
SCRIPT_DIR = os.path.dirname(__file__)

# Deployment-aware local model candidates:
# 1. source/models/modernbert-base 
#2. project-root/models/modernbert-base
# two paths build to connect to model
MODEL_DIR_CANDIDATES = [
    os.path.normpath(os.path.join(SCRIPT_DIR, '..', 'models', 'modernbert-base')),
    os.path.normpath(os.path.join(SCRIPT_DIR, '..', '..', 'models', 'modernbert-base')),
]
#default HF model
MODEL_SOURCE = MODEL_ID
for candidate in MODEL_DIR_CANDIDATES:
    if os.path.isdir(candidate) and os.path.exists(os.path.join(candidate, 'config.json')): #creates readable model name for logs and results
        MODEL_SOURCE = candidate
        break

if os.path.isdir(MODEL_SOURCE):
    MODEL_LABEL = os.path.basename(os.path.normpath(MODEL_SOURCE))
else:
    MODEL_LABEL = MODEL_SOURCE.split('/')[-1]
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu') #GPU if available (like home computer) server only uses CPU
TOKENIZER: Any = None #Tokenizer metadata used in all requests
MODEL: Any = None
MASK_TOKEN: str = ''
MASK_TOKEN_ID: Any = None

#initialize tokenizer/model once to keep in memory
# cuts CPU usage
def initialize_predictor_runtime(force_reload=False):
    """Load tokenizer/model once and keep them in memory for reuse."""
    global TOKENIZER, MODEL, MASK_TOKEN, MASK_TOKEN_ID

    if TOKENIZER is not None and MODEL is not None and not force_reload:
        return
#updated transformer pipline with autotokenizer 
# Model forward solving added one-pass multi mask inference
# New solution removes repeated per-blank model calls and reduces CPU latency
    TOKENIZER = AutoTokenizer.from_pretrained(MODEL_SOURCE)
    MODEL = AutoModelForMaskedLM.from_pretrained(MODEL_SOURCE)
    MODEL.to(DEVICE)
    MODEL.eval()

    mask_token_raw = getattr(TOKENIZER, 'mask_token', None)
    if not isinstance(mask_token_raw, str) or not mask_token_raw:
        raise RuntimeError('Tokenizer mask token is unavailable')
    MASK_TOKEN = mask_token_raw

    mask_token_id = getattr(TOKENIZER, 'mask_token_id', None)
    if mask_token_id is None:
        raise RuntimeError('Tokenizer mask token id is unavailable')
    MASK_TOKEN_ID = mask_token_id

    info_log(f"AI model loaded: {MODEL_SOURCE}")
    info_log(f"Runtime device: {DEVICE}")
    info_log(f"Torch threads: intra={torch.get_num_threads()} interop={TORCH_INTEROP_THREADS}")


# Initialize once at module load for CLI mode and local testing.
initialize_predictor_runtime()

# Word frequency ranking (higher = more common)
WORD_FREQUENCY_RANK = {
    # most-common words (rank 100+)
    "the": 100, "be": 100, "to": 100, "of": 100, "and": 100, "a": 100, "in": 100, "that": 100,
    "have": 100, "i": 100, "it": 100, "for": 100, "not": 100, "on": 100, "with": 100, "he": 100,
    "you": 100, "do": 100, "at": 100, "is": 105, "as": 98,
    
    # Very common words (rank 90)
    "this": 90, "but": 90, "his": 90, "by": 90, "from": 90, "they": 90, "we": 90, "say": 90,
    "her": 90, "she": 90, "or": 90, "an": 90, "will": 90, "my": 90, "one": 90, "all": 90,
    "would": 90, "there": 90, "their": 90, "what": 90, "test":90, "best":90,

 # Technology & Web words (rank 85)
    "website": 85, "internet": 85, "online": 85, "computer": 85, "email": 85, "web": 85,
    "digital": 85, "software": 85, "network": 85, "server": 85, "browser": 85,

# Academic/Testing words (rank 85)
"test": 85, "exam": 85, "quiz": 85, "study": 85, "class": 85, "school": 85,
"teacher": 85, "student": 85, "learn": 85, "read": 85, "write": 85, "book": 85,

# Time-related words (rank 75) 
"morning": 75, "afternoon": 75, "evening": 75, "night": 75, "today": 75,
"yesterday": 75, "tomorrow": 75, "week": 75, "month": 75, "year": 75,

# Emergency/Action words (rank 78)
"help": 78, "emergency": 78, "ambulance": 78, "danger": 78, "save": 78, "rescue": 78,

# Food domain words (rank 76)
"food": 76, "foods": 76, "bread": 76, "breads": 76, "sauce": 76, "sauces": 76,
"spread": 76, "spreads": 76, "beverage": 76, "beverages": 76, "condiment": 76, "condiments": 76,
"sugar": 76, "sweetened": 76, "dietary": 76,

# Animal/breeding terms (rank 76)
"breed": 76, "breeds": 76,

# Greetings (rank 72) 
"hello": 72, "hi": 72, "hey": 72, "goodbye": 72, "bye": 72, "thanks": 72,
"please": 72, "sorry": 72, "welcome": 72,

    # Common words (rank 80)
    "so": 80, "up": 80, "out": 80, "if": 80, "about": 80, "who": 80, "get": 80, "which": 80,
    "go": 80, "me": 80, "when": 80, "make": 80, "can": 80, "like": 80, "time": 80, "no": 80,
    "just": 80, "him": 80, "know": 80, "take": 80, "into": 80, "year": 80, "your": 80,
    "good": 80, "some": 80, "could": 80, "them": 80, "see": 80, "other": 80, "than": 80,
    "then": 80, "now": 80, "look": 80, "only": 80, "come": 80, "its": 80, "over": 80,
    "also": 80, "back": 80, "after": 80, "use": 80, "two": 80, "how": 80, "our": 80,
    "work": 80, "first": 80, "well": 80, "way": 80, "even": 80, "new": 80, "want": 80,
    "because": 80, "any": 80, "these": 80, "give": 80, "day": 80, "most": 80, "us": 80,
    "call": 80, "may": 80, "down": 80, "side": 80, "been": 80, "find": 80, "long": 80,
    
    # Context words words (rank 70)
    "boy": 70, "girl": 70, "man": 70, "woman": 70, "dog": 70, "cat": 70, "house": 70,
    "three": 70, "white": 70, "black": 70, "red": 70, "blue": 70, "green": 70, "old": 70,
    "big": 70, "small": 70, "high": 70, "low": 70, "large": 70, "little": 70, "young": 70,
    "bad": 70, "great": 70, "home": 70, "room": 70, "door": 70, "street": 70, "city": 70,
    "town": 70, "love": 70, "life": 70, "hand": 70, "place": 70, "case": 70, "week": 70,
    "play": 70, "help": 70, "turn": 70, "show": 70, "country": 70, "point": 70, "family": 70,
    "world": 70, "people": 70, "child": 70, "children": 70, "person": 70, "money": 70,
    "friend": 70, "game": 70, "idea": 70, "information": 70, "health": 70, "fire": 70,
    "police": 70, "robbers": 70, "pet": 70, "animal": 70, "landmass": 70, "continent": 70,
    "geography": 70, "population": 70, "inhabitants": 70, "industry": 70, "economy": 70, 
    "website": 70,
}
# Context Detection Keywords with boost multiplier for better awareness results
CONTEXT_KEYWORDS = {
    'emergency': {
        'keywords': ['fire', 'emergency', 'danger', 'alarm', 'urgent', 'accident', 'disaster', 
                     'burning', 'smoke', 'flames', 'help', '911', 'call'],
        'boost_words': ['call', 'help', 'fire', 'police', 'ambulance', 'firefighters', 
                        'emergency', 'evacuate', 'rescue', 'save', 'alert', 'notify'],
        'boost_multiplier': 3.0
    },
    'academic': {
        'keywords': ['test', 'exam', 'quiz', 'study', 'class', 'school', 'homework', 
                     'assignment', 'grade', 'teacher', 'student', 'learn'],
        'boost_words': ['test', 'exam', 'study', 'pass', 'fail', 'grade', 'score', 
                        'answer', 'question', 'write', 'read', 'prepare'],
        'boost_multiplier': 2.5
    },
    'greeting': {
        'keywords': ['good', 'hello', 'hi', 'hey', 'greetings'],
        'boost_words': ['morning', 'afternoon', 'evening', 'night', 'day', 'hello', 'hi'],
        'boost_multiplier': 3.5
    },
    'pet': {
        'keywords': ['bark', 'barks', 'woof', 'meow', 'purr', 'puppy', 'kitten', 
                     'pet', 'animal', 'vet', 'leash', 'collar', 'paw', 'tail'],
        'boost_words': ['dog', 'cat', 'pet', 'puppy', 'kitten', 'animal'],
        'boost_multiplier': 3.0
    },
    'time': {
        'keywords': ['today', 'yesterday', 'tomorrow', 'morning', 'afternoon', 'evening', 
                     'night', 'hour', 'minute', 'clock', 'time', 'day'],
        'boost_words': ['today', 'yesterday', 'tomorrow', 'morning', 'afternoon', 
                        'evening', 'night', 'now', 'later', 'soon'],
        'boost_multiplier': 2.0
    },
    'question': {
        'keywords': ['who', 'what', 'where', 'when', 'why', 'how', 'which'],
        'boost_words': ['is', 'are', 'was', 'were', 'do', 'does', 'did', 'can', 
                        'could', 'will', 'would', 'should'],
        'boost_multiplier': 2.0
    },
    'home': {
        'keywords': ['house', 'home', 'room', 'kitchen', 'bedroom', 'door', 'window'],
        'boost_words': ['house', 'home', 'room', 'door', 'window', 'kitchen', 'living'],
        'boost_multiplier': 2.2
    },
    'food': {
        'keywords': ['food', 'foods', 'diet', 'dietary', 'breakfast', 'lunch', 'dinner', 'snack',
                     'meal', 'sauce', 'sauces', 'condiment', 'condiments', 'beverage', 'beverages',
                     'drink', 'drinks', 'sugar', 'sweetened', 'ultraprocessed'],
        'boost_words': ['bread', 'breads', 'food', 'foods', 'sauce', 'sauces', 'spread', 'spreads',
                        'condiment', 'condiments', 'beverage', 'beverages', 'sugar', 'sweetened',
                        'breakfast', 'dinner', 'lunch', 'meal'],
        'boost_multiplier': 3.0
    }
}

FOOD_LOCAL_HINT_WORDS = {
    'food', 'foods', 'diet', 'dietary', 'breakfast', 'lunch', 'dinner', 'snack',
    'meal', 'sauce', 'sauces', 'condiment', 'condiments', 'beverage', 'beverages',
    'drink', 'drinks', 'sugar', 'sweetened', 'ultraprocessed'
}

FOOD_DOMAIN_CANDIDATES = {
    'bread', 'breads', 'food', 'foods', 'sauce', 'sauces', 'spread', 'spreads',
    'condiment', 'condiments', 'beverage', 'beverages', 'drink', 'drinks'
}

ANIMAL_BREED_CANDIDATES = {
    'breed', 'breeds', 'breeding', 'bred'
}
# Default small dictionary 
DEFAULT_DICTIONARY = {
    "king", "queen", "prince", "princess", "canada", "england", "scotland", "ireland", "wales",
    "love", "like", "hate", "live", "life", "time", "year", "have", "make", "take",
    "come", "give", "find", "tell", "work", "call", "hand", "part", "place", "case",
    "week", "company", "system", "program", "question", "government", "number", "night",
    "point", "home", "water", "room", "mother", "area", "money", "story", "fact", "month",
    "world", "family", "student", "group", "country", "problem", "school", "friend",
    "house", "service", "father", "power", "hour", "game", "line", "city", "name",
    "president", "team", "idea", "body", "information", "parent", "health", "person",
    "history", "party", "result", "change", "morning", "reason", "teacher", "education",
    "for", "from", "white", "three", "children", "book", "books", "firefighters",
    "cupcakes", "bookworm", "neighbour", "neighbor", "always", "the", "fire", "dog", "website", "best"
}
#context detection tool for emergency, academic, pets from sentences text
def keyword_in_sentence(sentence_lower, keyword):
    """Whole-word matching for single terms; substring for multi-word phrases."""
    key = str(keyword or '').strip().lower()
    if not key:
        return False

    if re.search(r'[^a-z0-9]', key):
        return key in sentence_lower

    return re.search(rf'\b{re.escape(key)}\b', sentence_lower) is not None

def detect_contexts(sentence):
    """Detect all applicable contexts in the sentence"""
    sentence_lower = sentence.lower() #case insensitive matching
    detected_contexts = []
    
    for context_name, context_data in CONTEXT_KEYWORDS.items():
        for keyword in context_data['keywords']:
            if keyword_in_sentence(sentence_lower, keyword):
                detected_contexts.append(context_name)
                debug_log(f"DEBUG: Detected '{context_name}' context (keyword: '{keyword}')", 
                      file=sys.stderr)
                break  # Only need one keyword match per context
    
    return detected_contexts

def extract_local_context_window(sentence, start, end, window_size=48):
    """Extract local context around the current missing-letter pattern"""
    sentence_text = str(sentence or '')
    left = max(0, int(start) - int(window_size))
    right = min(len(sentence_text), int(end) + int(window_size))
    return sentence_text[left:right].lower()

def apply_domain_disambiguation(word, ranking_score, detected_contexts, local_context):
    """
    Apply small domain-aware tie-breaking in ambiguous cases
    """
    adjusted_score = float(ranking_score)
    word_lower = str(word or '').lower()
    local_lower = str(local_context or '').lower()
    has_food_local_hint = any(keyword_in_sentence(local_lower, hint) for hint in FOOD_LOCAL_HINT_WORDS)

    if 'food' in detected_contexts and has_food_local_hint:
        if word_lower in FOOD_DOMAIN_CANDIDATES:
            adjusted_score += 120.0
        if word_lower in ANIMAL_BREED_CANDIDATES:
            adjusted_score -= 90.0

    return adjusted_score
#multiplies confidence if predicted wird is in context boost list
def apply_context_boost(word, confidence, detected_contexts, sentence):
    """Apply context-based confidence boosts to predictions"""
    original_confidence = confidence
    boost_applied = False
    
    word_lower = word.lower()
    
    for context_name in detected_contexts:
        context_data = CONTEXT_KEYWORDS.get(context_name)
        if not context_data:
            continue
            
        if word_lower in [w.lower() for w in context_data['boost_words']]:
            confidence *= context_data['boost_multiplier']
            boost_applied = True
            debug_log(f"DEBUG: Boosted '{word}' from {original_confidence:.2f}% to {confidence:.2f}% "
                  f"({context_name} context)", file=sys.stderr)
    
    return confidence, boost_applied

#dictionary loader
def load_dictionary(filepath):
    """Load dictionary from file"""
    if not filepath or not os.path.exists(filepath):
        info_log(f"Dictionary file not found, using default ({len(DEFAULT_DICTIONARY)} words)")
        return DEFAULT_DICTIONARY
    
    try:
        words = set()
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                word = line.strip().lower()
                if word and word.isalpha():
                    words.add(word)
        
        info_log(f"Dictionary loaded: {len(words):,} words")
        return words
    
    except Exception as e:
        info_log(f"Dictionary loading error: {e}")
        info_log(f"Using default dictionary ({len(DEFAULT_DICTIONARY)} words)")
        return DEFAULT_DICTIONARY
    
#pattern matching for missing letters
def matches_pattern(word, pattern):
    """Check if a word matches a pattern with underscores"""
    if len(word) != len(pattern):
        return False
    
    for w_char, p_char in zip(word, pattern):
        if p_char != '_':
            if w_char.lower() != p_char.lower():
                return False
    
    return True

#
def get_dictionary_indexes(dictionary):
    """Build and cache lightweight indexes for faster candidate generation"""
    cache_key = id(dictionary)
    cached = DICTIONARY_INDEX_CACHE.get(cache_key)
    if cached is not None:
        return cached
#lookup map created and filtered by following parameters
    by_length = {}
    by_length_first = {}
    by_length_last = {}
    by_length_edges = {}
#pass through dictionary once and builds index and turns candidate lookup to scanning smaller portions
    for raw_word in dictionary:
        word = str(raw_word).lower()
        if not word:
            continue
        length = len(word)
        first = word[0]
        last = word[-1]

        by_length.setdefault(length, []).append(word)
        by_length_first.setdefault((length, first), []).append(word)
        by_length_last.setdefault((length, last), []).append(word)
        by_length_edges.setdefault((length, first, last), []).append(word)

    indexes = {
        'by_length': by_length,
        'by_length_first': by_length_first,
        'by_length_last': by_length_last,
        'by_length_edges': by_length_edges,
    }
    DICTIONARY_INDEX_CACHE[cache_key] = indexes
    return indexes  #return built index

# exact pattern mcache, supports LRU behaviour and is thread-safe
def get_pattern_candidates(pattern_lower, dictionary):
    """Return candidate words for a pattern using indexes + cached exact pattern matches."""
    cache_key = (id(dictionary), pattern_lower)
    with LETTER_PATTERN_CACHE_LOCK:
        cached = LETTER_PATTERN_CACHE.get(cache_key)
        if cached is not None:
            LETTER_PATTERN_CACHE.move_to_end(cache_key)
            return cached
#extract known constratints from pattern by total lnegth always known and first/last (if known)
    indexes = get_dictionary_indexes(dictionary)
    length = len(pattern_lower)
    first = pattern_lower[0] if pattern_lower and pattern_lower[0] != '_' else None
    last = pattern_lower[-1] if pattern_lower and pattern_lower[-1] != '_' else None
#smallest possible candidate pool extracted for speed boost
    if first and last:
        pool = indexes['by_length_edges'].get((length, first, last), [])
    elif first:
        pool = indexes['by_length_first'].get((length, first), [])
    elif last:
        pool = indexes['by_length_last'].get((length, last), [])
    else:
        pool = indexes['by_length'].get(length, [])

    matches = [word for word in pool if matches_pattern(word, pattern_lower)] #final extract filter with matches_pattern
    matches.sort(key=lambda w: (-WORD_FREQUENCY_RANK.get(w.lower(), 0), w.lower())) #ranking sort descending first, then alphabetic 

    with LETTER_PATTERN_CACHE_LOCK:
        LETTER_PATTERN_CACHE[cache_key] = matches
        LETTER_PATTERN_CACHE.move_to_end(cache_key)
        while len(LETTER_PATTERN_CACHE) > LETTER_PATTERN_CACHE_MAX:
            LETTER_PATTERN_CACHE.popitem(last=False)

    return matches #Matches are returned

#function to compute request complexity for number of missing words, total gaps counted and total sentence length
def build_mixed_runtime_options(sentence, words_with_gaps, mode):
    """Build runtime options for mixed mode to reduce workload on heavy sentences"""
    missing_word_count = str(sentence).count('___')
    total_gaps = len(words_with_gaps) + missing_word_count
    sentence_len = len(str(sentence))
    is_heavy = (
        mode != 'top' and (
            sentence_len >= MIXED_HEAVY_SENTENCE_LEN or
            total_gaps >= MIXED_HEAVY_TOTAL_GAPS or
            missing_word_count >= 4
        )
    )
    return {
        'is_heavy': is_heavy,       #whether a sentence is heavy 
        'word_top_k': MIXED_FAST_WORD_TOP_K if is_heavy else 20,
        'word_scan_limit': MIXED_FAST_WORD_SCAN_LIMIT if is_heavy else 20,
        'letter_fast_mode': is_heavy,
    }

#fast path for heavy mixed sentences by taking top pool window
def build_fast_letter_ranked_predictions(
    matching_words,
    pattern_length,
    detected_contexts,
    sentence,
    local_context
):
    """Fast dictionary/context ranking path used for heavy mixed top3 requests.""" #fast path for heavy mixed predictions
    scored_matches = []
    for match in matching_words[:LETTER_FAST_RANK_POOL]:
        match_lower = match.lower()
        word_rank = WORD_FREQUENCY_RANK.get(match_lower, 0)
        frequency_boost = word_rank if word_rank > 0 else 0
        length_bonus = 10 if pattern_length >= 5 else 0
#heuristic for very common function words
        ultra_common = ["for", "the", "and", "but", "not", "are", "was", "his", "her", "can", "has", "is"]
        if match_lower in ultra_common and pattern_length <= 3:
            frequency_boost = 100
    #applies context/domain boost to ranking scores
        ranking_score = max(1.0, float(frequency_boost + length_bonus))
        ranking_score, _ = apply_context_boost(
            match_lower,
            ranking_score,
            detected_contexts,
            sentence
        )
        ranking_score = apply_domain_disambiguation( #stores ranking scores 
            match_lower,
            ranking_score,
            detected_contexts,
            local_context
        )

        scored_matches.append({
            'word': match,
            'confidence': 0.0,
            'method': 'Dictionary Fast Rank',
            '_ranking_score': ranking_score
        })
#converts confidence from 0-100
    scored_matches.sort(key=lambda x: x['_ranking_score'], reverse=True)
    fallback_confidences = normalized_rank_confidences(
        [item['_ranking_score'] for item in scored_matches]
    )
    for item, fallback_conf in zip(scored_matches, fallback_confidences): #replaces internal ranking score with user facing confidence
        item['confidence'] = round(min(100.0, max(0.0, fallback_conf)), 2)
        item.pop('_ranking_score', None)

    return scored_matches[:MAX_OUTPUT_PREDICTIONS] #Returns only N top output for top 3 mode


#missing letter prediction which checks if word matches underscore pattern
def find_words_with_missing_letters(sentence):
    """Find words with missing letters, excluding standalone ___"""
    pattern = r'\b[a-zA-Z_]+\b'
    words_with_gaps = []
    
    for match in re.finditer(pattern, sentence):
        matched_text = match.group()
        
        # Must have both letters AND underscores
        has_letters = any(c.isalpha() for c in matched_text)
        has_underscores = '_' in matched_text
        
        # Skip if it's only letters (normal word) or only underscores or if it's ___
        if has_letters and has_underscores and matched_text != '___':
            words_with_gaps.append({
                'pattern': matched_text,
                'start': match.start(),
                'end': match.end()
            })
    
    return words_with_gaps
#compute model probability with softmax 
#
def get_mask_vocab_probabilities(masked_sentence):
    """
    Return full-vocabulary probabilities for a sentence containing exactly one mask token
    """
    initialize_predictor_runtime()
    encoded = TOKENIZER(masked_sentence, return_tensors='pt', truncation=True)
    encoded = {key: value.to(DEVICE) for key, value in encoded.items()}

    mask_positions = (encoded['input_ids'][0] == MASK_TOKEN_ID).nonzero(as_tuple=True)[0]
    if mask_positions.numel() != 1:
        raise ValueError("Expected exactly one mask token for missing-letter scoring")

    with torch.no_grad():
        logits = MODEL(**encoded).logits

    mask_index = int(mask_positions[0].item())
    return torch.softmax(logits[0, mask_index], dim=-1).detach().cpu()
#candidate word probability p is extracted from vocab probs (single-token mapping) to fix bug where letter confidence was always 0
def get_token_probability(vocab_probs, word):
    """
Get probability for a candidate word token from full-vocabulary probabilities
    Returns 0 when a word does not map to a single token
    """
    candidate_token_ids = set()
    candidate_forms = {word, word.lower(), f" {word}", f" {word.lower()}"}

    for token_form in candidate_forms:
        token_ids = TOKENIZER.encode(token_form, add_special_tokens=False)
        if len(token_ids) == 1:
            candidate_token_ids.add(token_ids[0])

    if not candidate_token_ids:
        return 0.0

    return max(float(vocab_probs[token_id].item()) for token_id in candidate_token_ids)
#avg_log = (1/n) * Σ log(p_i)
# confidence = exp(avg_log)
# clamped to [0,1]
def geometric_mean_probability(probabilities):
    """
    Compute geometric mean (solution B) for a list of probabilities in [0, 1]
    """
    if not probabilities:
        return 0.0

    epsilon = 1e-12
    safe_probs = [min(1.0, max(0.0, float(p))) for p in probabilities]
    avg_log = sum(math.log(max(p, epsilon)) for p in safe_probs) / len(safe_probs)
    return min(1.0, max(0.0, math.exp(avg_log)))

def normalized_rank_confidences(scores):
    """
    Convert ranking scores into a 0-100 normalized confidence distribution
    Used as a fallback when token probabilities are unavailable for all candidates
    """
    if not scores:
        return []

    max_score = max(scores)
    exp_scores = [math.exp(float(score) - float(max_score)) for score in scores]
    total = sum(exp_scores)
    if total <= 0:
        return [0.0 for _ in scores]

    return [round((value / total) * 100.0, 2) for value in exp_scores]
# build candidate list from dictionary via pattern matching
def predict_missing_letters(sentence, word_info, dictionary, fast_mode=False):
    """Predict missing letters using dictionary + BERT context"""
    pattern = word_info['pattern']
    pattern_lower = pattern.lower()
    pattern_length = len(pattern)
    
    debug_log(f"DEBUG: Looking for pattern '{pattern}' (length: {pattern_length})")
    detected_contexts = detect_contexts(sentence)
    local_context = extract_local_context_window(
        sentence,
        word_info.get('start', 0),
        word_info.get('end', 0)
    )
    matching_words = get_pattern_candidates(pattern_lower, dictionary)
    debug_log(f"DEBUG: Found {len(matching_words)} matching words: {matching_words[:10]}")
    
    if not matching_words:
        return [{
            'word': 'No match found',
            'confidence': 0,
            'method': 'Failed',
            'note': f'No dictionary words match pattern "{pattern}"'
        }]

    if fast_mode:
        return build_fast_letter_ranked_predictions(
            matching_words=matching_words,
            pattern_length=pattern_length,
            detected_contexts=detected_contexts,
            sentence=sentence,
            local_context=local_context
        )
    
    # repalce pattern with mask toke for model context
    masked_sentence = (
        sentence[:word_info['start']] + 
        MASK_TOKEN + 
        sentence[word_info['end']:]
    )
    
    debug_log(f"DEBUG: Masked sentence: {masked_sentence}")
    
    try:
        vocab_probs = get_mask_vocab_probabilities(masked_sentence)
        _, top_ids = torch.topk(vocab_probs, k=5)
        top_words = [TOKENIZER.decode([int(token_id)]).strip().lower() for token_id in top_ids.tolist()]
        debug_log(f"DEBUG: BERT top 5: {top_words}")
        
        scored_matches = []
        mask_count = max(1, pattern.count('_'))
        
        for match in matching_words:
            match_lower = match.lower()
            token_probability = get_token_probability(vocab_probs, match_lower)

            if token_probability > 0:
                # For UI confidence, aggregate probabilities in log space
                display_probability = geometric_mean_probability([token_probability] * mask_count)
                method = "AI Context Match"
            else:
                display_probability = 0.0
                method = "Dictionary Only"

            display_confidence = round(display_probability * 100, 2)
            
            word_rank = WORD_FREQUENCY_RANK.get(match_lower, 0)
            frequency_boost = word_rank if word_rank > 0 else 0
            
            length_bonus = 10 if pattern_length >= 5 else 0
            
            ultra_common = ["for", "the", "and", "but", "not", "are", "was", "his", "her", "can", "has", "is"]
            if match_lower in ultra_common and pattern_length <= 3:
                frequency_boost = 100
            # Build ranking score for candidate ordering
            # Displayed confidence remains model-driven and clamped separately
            ranking_score = display_confidence + frequency_boost + length_bonus
            if token_probability > 0 and match_lower in top_words[:3]:
                ranking_score += 20
            elif token_probability <= 0 and pattern_length <= 3 and frequency_boost > 0:
                ranking_score = max(ranking_score, 80)

            # Context boost applies to ranking (not displayed confidence)
            ranking_score, _ = apply_context_boost(
                match_lower,
                max(1.0, ranking_score),
                detected_contexts,
                sentence
            )
            ranking_score = apply_domain_disambiguation(
                match_lower,
                ranking_score,
                detected_contexts,
                local_context
            )
            
            scored_matches.append({
                'word': match,
                'confidence': round(min(100.0, max(0.0, display_confidence)), 2),
                'method': method,
                '_ranking_score': ranking_score,
                '_token_probability': float(token_probability)
            })
        
        scored_matches.sort(key=lambda x: x['_ranking_score'], reverse=True)

        # If model probabilities are unavailable or all displayed confidences round down to zero, fall back to normalized rank confidence
        #fixed bug where letter confidence was always 0
        no_token_probs = not any(item.get('_token_probability', 0.0) > 0 for item in scored_matches)
        no_visible_confidence = not any(float(item.get('confidence', 0.0)) > 0.0 for item in scored_matches)
        if no_token_probs or no_visible_confidence:
            fallback_confidences = normalized_rank_confidences(
                [item['_ranking_score'] for item in scored_matches]
            )
            for item, fallback_conf in zip(scored_matches, fallback_confidences):
                item['confidence'] = round(min(100.0, max(0.0, fallback_conf)), 2)
                item['method'] = 'Dictionary Rank'

        for item in scored_matches:
            item.pop('_ranking_score', None)
            item.pop('_token_probability', None)
        
        debug_log(f"DEBUG: Top match: {scored_matches[0]['word']} (confidence: {scored_matches[0]['confidence']})")
        
        return scored_matches[:MAX_OUTPUT_PREDICTIONS]
    #fallback on exception dictionary only results
    except Exception as e:
        debug_log(f"DEBUG: Exception: {e}")
        result = []
        for word in matching_words[:MAX_OUTPUT_PREDICTIONS]:
            result.append({
                'word': word,
                'confidence': 0.0,
                'method': 'Dictionary Only'
            })
        return result
    
EMERGENCY_KEYWORDS = [
    'fire', 'emergency', 'help', 'danger', 'alarm', 'urgent', 'accident', 'disaster'
]

ACTION_VERBS = [
    'call', 'alert', 'notify', 'contact', 'summon', 'get', 'fetch', 'find'
]

PET_KEYWORDS = {
    'dog': ['bark', 'barks', 'barked', 'barking', 'woof', 'puppy', 'puppies', 'fetch', 'leash', 'walk', 'paw', 'tail'],
    'cat': ['meow', 'meows', 'meowed', 'meowing', 'purr', 'purrs', 'kitten', 'kittens', 'whisker', 'litter'],
    'bird': ['chirp', 'chirps', 'tweet', 'tweets', 'fly', 'flies', 'wing', 'wings', 'feather', 'nest'],
    'fish': ['swim', 'swims', 'tank', 'aquarium', 'fins', 'scales'],
    'pet': ['animal', 'vet', 'veterinarian', 'collar', 'food bowl', 'pet food']
}

BASE_EXCLUDED_TOKENS = {'.', ',', '!', '?', ';', ':', '-', '...'}
SECOND_PERSON_WORDS = {'you', 'your', 'yours', 'yourself', 'yourselves'}
FIRST_PERSON_WORDS = {'my', 'mine', 'me', 'myself', 'our', 'ours'}
POSSESSIVE_HINT_WORDS = {
    'dog', 'cat', 'house', 'car', 'name', 'mother', 'father', 'friend', 'phone', 'garden',
    'book', 'idea', 'family', 'home', 'job', 'school', 'teacher'
}
SINGLE_CHAR_ALLOWED_TOKENS = {'a', 'i'}

# improved candidate filtering  and mode normalization
# Includes strict token filtering so invalid MLM tokens never reach output and normalized modes
def normalize_mode_value(mode):
    """Normalize external mode aliases to internal values."""
    normalized = str(mode or '').strip().lower()
    if normalized == 'top3':
        return 'top3'
    # keep backward compatibility: 'best' alias maps to top-1 mode
    if normalized in {'best', 'top'}:
        return 'top'
    return 'top'

# Improving accuracy by removing transformer special tokens like [CLS], [SEP], [MASK], [PAD], [UNK]
# 
def is_special_bracket_token(token):
    token_text = str(token or '').strip()
    return token_text.startswith('[') and token_text.endswith(']')

# Strict validation for candidate tokens before they enter final output ranking. Rejecting:
# - empty/whitespace
# - special bracket tokens
# - wordpiece fragments (##...)
# - tokens containing whitespace
# - underscore-only artifacts
# - punctuation/symbol-only tokens
# - very short noise tokens (except 'a' and 'i')
def is_valid_candidate(token: str) -> bool:
    """Return True only for natural-language candidate tokens."""
    token_text = str(token or '')
    if not token_text or not token_text.strip():
        return False

    token_text = token_text.strip()

    # Reject model/special bracket tokens such as [CLS], [SEP], [MASK], [PAD], [UNK]
    if is_special_bracket_token(token_text):
        return False

    # Reject wordpiece continuations
    if '##' in token_text:
        return False

    # Reject whitespace-containing tokens
    if re.search(r'\s', token_text):
        return False

    # Reject underscore-only artifacts like "_" / "__"
    if re.fullmatch(r'_+', token_text):
        return False

    # Reject punctuation/symbol-only candidates
    if re.fullmatch(r"[^\w]+", token_text):
        return False

    # Optional short-token guard
    if len(token_text) < 2 and token_text.lower() not in SINGLE_CHAR_ALLOWED_TOKENS:
        return False

    return True

# Fallback validator used only when strict filtering removes everything.
# blocks transformer artifact tokens to improve output quality
def is_safe_fallback_candidate(token: str) -> bool:
    """Fallback token guard: permissive but still blocks special and fragment artifacts."""
    token_text = str(token or '').strip()
    if not token_text:
        return False
    if is_special_bracket_token(token_text):
        return False
    if '##' in token_text:
        return False
    if re.search(r'\s', token_text):
        return False
    if re.fullmatch(r'_+', token_text):
        return False
    return True

def detect_pet_context(sentence):
    """Detect pet-related context label from sentence text."""
    sentence_lower = sentence.lower()
    for pet_type, keywords in PET_KEYWORDS.items():
        if any(keyword in sentence_lower for keyword in keywords):
            return pet_type
    return None

def detect_direct_address_context(sentence):
    """Detect direct-address phrasing (e.g., 'Hello Tom.')."""
    sentence_text = sentence.strip()
    if not sentence_text:
        return False

    greeting_pattern = r"^\s*(hello|hi|hey|dear)\s+[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*)?[,.!?]"
    vocative_pattern = r"^\s*[A-Za-z][A-Za-z'\-]*(?:\s+[A-Za-z][A-Za-z'\-]*)?,\s"

    return bool(re.search(greeting_pattern, sentence_text, flags=re.IGNORECASE) or re.search(vocative_pattern, sentence_text))

def collect_word_predictions(
    predictions,
    pet_context,
    has_emergency_context=False,
    has_direct_address_context=False,
    masked_sentence='',
    blank_position=None,
    exclude_tokens=None,
    debug_prefix='',
    include_generic_pet_boost=False,
    max_candidates_scan=20
):
    """Normalize BERT predictions and apply branch-specific boosts."""
    if exclude_tokens is None:
        exclude_tokens = BASE_EXCLUDED_TOKENS

    predicted_words = []
    scan_limit = max(MAX_OUTPUT_PREDICTIONS, min(20, int(max_candidates_scan)))

    for pred in predictions[:scan_limit]:
        word = normalize_model_token(pred.get('token_str', ''))
        if is_fragment_prediction(word):
            continue
        base_confidence = pred['score'] * 100
        confidence = min(100.0, max(0.0, base_confidence))
        ranking_score = confidence

        if has_emergency_context and word.lower() in ACTION_VERBS:
            ranking_score *= 5.0
            debug_log(
                f"DEBUG{debug_prefix}: Boosting ranking for '{word}' from {base_confidence:.2f}% to {ranking_score:.2f}% (emergency context)",
                file=sys.stderr
            )

        if pet_context and word.lower() == pet_context:
            ranking_score *= 3.0
            debug_log(
                f"DEBUG{debug_prefix}: Boosting ranking for '{word}' from {base_confidence:.2f}% to {ranking_score:.2f}% (pet context)",
                file=sys.stderr
            )

        if include_generic_pet_boost and pet_context and word.lower() == 'pet':
            ranking_score *= 2.0
            debug_log(
                f"DEBUG{debug_prefix}: Boosting ranking for '{word}' from {base_confidence:.2f}% to {ranking_score:.2f}% (generic pet)",
                file=sys.stderr
            )

        if has_direct_address_context:
            lowered_masked_sentence = masked_sentence.lower() if masked_sentence else ''
            mask_hint = MASK_TOKEN.lower()

            if word.lower() in SECOND_PERSON_WORDS:
                boost = 2.5
                if (
                    word.lower() in {'your', 'yours'} and
                    any(f"{mask_hint} {hint}" in lowered_masked_sentence for hint in POSSESSIVE_HINT_WORDS)
                ):
                    boost = 5.0
                ranking_score *= boost
                debug_log(
                    f"DEBUG{debug_prefix}: Boosting ranking for '{word}' from {base_confidence:.2f}% to {ranking_score:.2f}% (direct address context)",
                    file=sys.stderr
                )
            elif word.lower() in FIRST_PERSON_WORDS:
                ranking_score *= 0.35
                debug_log(
                    f"DEBUG{debug_prefix}: Downweighting ranking for '{word}' from {base_confidence:.2f}% to {ranking_score:.2f}% (direct address context)",
                    file=sys.stderr
                )

        if word in exclude_tokens:
            continue

        item = {
            'word': word,
            'confidence': round(confidence, 2),
            'method': 'BERT AI',
            '_ranking_score': ranking_score
        }
        if blank_position is not None:
            item['blank_position'] = blank_position
        predicted_words.append(item)

    predicted_words.sort(key=lambda x: x['_ranking_score'], reverse=True)
    for item in predicted_words:
        item.pop('_ranking_score', None)
    return predicted_words

#missing words prediction for ___ in sentences 
# Predict all missing words in one model forward pass
# 1. replace every ___ with MASK token
# 2. run tokenizer/model once
# 3. extract logits at each mask position
# 4. compute softmax probabilities and top-k per mask
# New solution removes repeated per-blank model calls and reduces CPU latency
def predict_missing_words_multi_mask(sentence, mode='top', runtime_options=None):
    """Predict missing words for all blanks using a single model forward pass."""
    initialize_predictor_runtime()

    runtime_options = runtime_options or {}
    mode = normalize_mode_value(mode)

    word_top_k = max(MAX_OUTPUT_PREDICTIONS, min(20, int(runtime_options.get('word_top_k', 20))))
    word_scan_limit = max(MAX_OUTPUT_PREDICTIONS, min(20, int(runtime_options.get('word_scan_limit', 20))))

#multi-mask loop with over-generation and filter applied after
    raw_candidate_top_k = max(50, word_top_k)
    output_limit = 1 if mode == 'top' else MAX_OUTPUT_PREDICTIONS

    sentence = str(sentence or '').strip()
    blank_count = sentence.count('___')
    if blank_count == 0:
        return []

    if sentence.endswith('___') and sentence[-4:-3] not in '.!?,;:':
        sentence = sentence + ' .'

    masked_sentence = sentence.replace('___', MASK_TOKEN)
    debug_log(f"DEBUG multi-mask sentence: '{masked_sentence}'", file=sys.stderr)

    encoded = TOKENIZER(masked_sentence, return_tensors='pt', truncation=True)
    encoded = {key: value.to(DEVICE) for key, value in encoded.items()}
    input_ids = encoded['input_ids'][0]
    mask_positions = (input_ids == MASK_TOKEN_ID).nonzero(as_tuple=True)[0]

    if mask_positions.numel() == 0:
        return []

    with torch.no_grad():
        outputs = MODEL(**encoded)
    logits = outputs.logits[0]  # [seq_len, vocab]

    has_emergency_context = any(keyword in masked_sentence.lower() for keyword in EMERGENCY_KEYWORDS)
    has_direct_address_context = detect_direct_address_context(masked_sentence)
    pet_context = detect_pet_context(masked_sentence)

    excluded_tokens = set(BASE_EXCLUDED_TOKENS)
    all_predictions = []
#compute calibrated token probability at each masked position
# Probs are mapped 0 to 1 and displayed as 0 to 100 by confidence
    for idx, pos in enumerate(mask_positions.tolist(), start=1):
        logits_at_pos = logits[pos]
        probs = torch.softmax(logits_at_pos, dim=-1)
        current_k = min(int(probs.shape[0]), raw_candidate_top_k)
        top_probs, top_ids = torch.topk(probs, k=current_k)

        raw_predictions = []
        fallback_non_special = None

        for rank_idx, (token_prob, token_id) in enumerate(zip(top_probs.tolist(), top_ids.tolist())):
            token_piece = TOKENIZER.convert_ids_to_tokens(int(token_id))
            if token_piece is None:
                token_piece = ''
            token_piece = str(token_piece)

            if fallback_non_special is None and is_safe_fallback_candidate(token_piece):
                fallback_non_special = {
                    'token_str': normalize_model_token(token_piece),
                    'score': float(token_prob),
                    'filtered_empty_fallback': True,
                }

            # Required debug line when a special token appears in top-3 and gets filtered
            #logged to STDERR
            if rank_idx < 3 and is_special_bracket_token(token_piece):
                debug_log(f"FILTER: removed {token_piece} at mask #{idx}", file=sys.stderr)

            if not is_valid_candidate(token_piece):
                continue

            token_str = normalize_model_token(token_piece)
            if not is_valid_candidate(token_str):
                continue

            raw_predictions.append({
                'token_str': token_str,
                'score': float(token_prob),
            })

        debug_log(
            f"DEBUG multi-mask blank {idx}: {[(p['token_str'], round(p['score'] * 100, 2)) for p in raw_predictions[:10]]}",
            file=sys.stderr
        )

        predicted_words = collect_word_predictions(
            predictions=raw_predictions,
            pet_context=pet_context,
            has_emergency_context=has_emergency_context,
            has_direct_address_context=has_direct_address_context,
            masked_sentence=masked_sentence,
            blank_position=idx,
            exclude_tokens=excluded_tokens,
            debug_prefix=' multi-mask',
            include_generic_pet_boost=True,
            max_candidates_scan=word_scan_limit
        )

        if not predicted_words:
            safe_fallback = None
            if fallback_non_special and is_safe_fallback_candidate(fallback_non_special.get('token_str', '')):
                safe_fallback = fallback_non_special
            else:
                for token_prob, token_id in zip(top_probs.tolist(), top_ids.tolist()):
                    token_piece = str(TOKENIZER.convert_ids_to_tokens(int(token_id)) or '')
                    if not is_safe_fallback_candidate(token_piece):
                        continue
                    token_str = normalize_model_token(token_piece)
                    if not token_str:
                        continue
                    safe_fallback = {
                        'token_str': token_str,
                        'score': float(token_prob),
                        'filtered_empty_fallback': True, #tight filtering applied
                    }
                    break
                    # fallback behaviour and optional markers
            if safe_fallback:
                predicted_words = [{
                    'word': safe_fallback['token_str'],
                    'confidence': round(min(100.0, max(0.0, safe_fallback['score'] * 100)), 2),
                    'method': 'BERT AI',
                    'filtered_empty_fallback': True,
                }]
            else:
                predicted_words = [{
                    'word': 'unknown',
                    'confidence': 0.0,
                    'method': 'BERT AI',
                    'filtered_empty_fallback': True,
                }]

        all_predictions.append({
            'position': idx,
            'predictions': predicted_words[:output_limit]
        })

    return all_predictions

# Wrapper keeps older response shape by
# - single blank -> flat predictions list
# - multiple blanks -> list grouped by blank position
def predict_missing_words(sentence, mode='top', runtime_options=None):
    """Backward-compatible wrapper for missing-word prediction."""
    try:
        all_predictions = predict_missing_words_multi_mask(sentence, mode=mode, runtime_options=runtime_options)
        if not all_predictions:
            return []

        blank_count = str(sentence or '').count('___')
        if blank_count <= 1:
            first = all_predictions[0].get('predictions', [])
            return first if first else [{'word': 'No confident token', 'confidence': 0.0, 'method': 'BERT AI'}]
        return all_predictions
    except Exception as e:
        return [{'error': str(e)}]


def run_multi_mask_shape_self_check():
    """Quick shape checks for multi-mask output contract."""
    samples = {
        'single_blank': "Good ___ morning.",
        'three_blanks': "___ I ___ to the ___ store.",
        'long_paragraph': (
            "In this ___ paragraph, we try to ___ multiple blanks and verify that the ___ output "
            "structure remains stable while the model handles longer context windows."
        ),
    }

    report = {}
    for name, sentence in samples.items():
        result_best = predict_missing_words_multi_mask(sentence, mode='top', runtime_options={})
        result_top3 = predict_missing_words_multi_mask(sentence, mode='top3', runtime_options={})
        report[name] = {
            'blank_count': sentence.count('___'),
            'best_positions': len(result_best),
            'top3_positions': len(result_top3),
            'best_shape_ok': all('position' in item and 'predictions' in item for item in result_best),
            'top3_shape_ok': all('position' in item and 'predictions' in item for item in result_top3),
        }
    return report

# STDERR only self check helper in new candidate filtering and mode normalization
#filtering applied without touching normalized JSON response
def run_candidate_filter_self_check():
    """STDERR-only self-check: show candidate filtering before/after for one mask."""
    initialize_predictor_runtime()
    sentence = "Good ___ morning."
    masked_sentence = sentence.replace('___', MASK_TOKEN)

    encoded = TOKENIZER(masked_sentence, return_tensors='pt', truncation=True)
    encoded = {key: value.to(DEVICE) for key, value in encoded.items()}
    input_ids = encoded['input_ids'][0]
    mask_positions = (input_ids == MASK_TOKEN_ID).nonzero(as_tuple=True)[0]
    if mask_positions.numel() == 0:
        info_log("FILTER SELF-CHECK: no mask position found", file=sys.stderr)
        return

    with torch.no_grad():
        logits = MODEL(**encoded).logits[0]

    pos = int(mask_positions[0].item())
    probs = torch.softmax(logits[pos], dim=-1)
    k = min(20, int(probs.shape[0]))
    top_probs, top_ids = torch.topk(probs, k=k)

    before = []
    after = []
    for token_prob, token_id in zip(top_probs.tolist(), top_ids.tolist()):
        token_piece = str(TOKENIZER.convert_ids_to_tokens(int(token_id)) or '')
        before.append(token_piece)
        if not is_valid_candidate(token_piece):
            continue
        normalized = normalize_model_token(token_piece)
        if not is_valid_candidate(normalized):
            continue
        after.append((normalized, round(float(token_prob) * 100, 2)))

    info_log(f"FILTER SELF-CHECK before: {before}", file=sys.stderr)
    info_log(f"FILTER SELF-CHECK after: {after}", file=sys.stderr)

#output mode control which handles flat lists and nested blank structure
#keeps best mode light but still exposes top K options to foster trust and explainability
def limit_predictions(predictions, mode):
    """Limit predictions based on output mode"""
    mode = normalize_mode_value(mode)
    if not predictions:
        return predictions
    #reconstructed sentence is generated only in top mode because top3 is intentionally ambiguous for final sentence
    if isinstance(predictions, list) and len(predictions) > 0:
        if isinstance(predictions[0], dict) and 'position' in predictions[0]:
            result = []
            for blank_data in predictions:
                limited_blank = blank_data.copy()
                if 'predictions' in limited_blank:
                    if mode == 'top':
                        all_preds = limited_blank['predictions'][:MAX_OUTPUT_PREDICTIONS]
                        if all_preds:
                            primary = all_preds[0].copy()
                            if len(all_preds) > 1:
                                primary['alternatives'] = all_preds[1:MAX_OUTPUT_PREDICTIONS]
                            limited_blank['predictions'] = [primary]
                        else:
                            limited_blank['predictions'] = []
                    elif mode == 'top3':
                        limited_blank['predictions'] = limited_blank['predictions'][:MAX_OUTPUT_PREDICTIONS]
                    else:
                        limited_blank['predictions'] = limited_blank['predictions'][:1]
                result.append(limited_blank)
            return result
    
    if mode == 'top':
        top = predictions[:MAX_OUTPUT_PREDICTIONS]
        if not top:
            return top
        primary = top[0].copy()
        if len(top) > 1:
            primary['alternatives'] = top[1:MAX_OUTPUT_PREDICTIONS]
        return [primary]
    elif mode == 'top3':
        return predictions[:MAX_OUTPUT_PREDICTIONS]
    else:
        return predictions[:1]

def normalize_model_token(token):
    """
    Normalize model token strings to reduce word piece artifacts in UI output
    Re-joins split suffixes -> sleeped (or intended suffix-join behavior), overall reducing broken output
    """
    text = str(token or '').strip()
    text = text.replace('##', '')
    text = text.replace('▁', '')
    text = text.replace('Ġ', '')
    text = text.replace('â–', '')
    text = text.replace('Ä ', '')
    return re.sub(r'\s+', ' ', text).strip()

def is_fragment_prediction(word):
    """
    Filter out fragment-like predictions that are usually tokenizer artifacts
    """
    if not word:
        return True

    lowered = word.lower().strip()
    if lowered in {'##', '#', '[unk]', '<unk>', '``', "''"}:
        return True

    if lowered in {'ed', 'ing', 'ly'}:
        return True

    if re.fullmatch(r'[#_]+', lowered):
        return True

    if re.search(r'\d', lowered):
        return True

    return False
    
#apply capitalization rules for better readability, correctness and format
def capitalize_sentence(sentence):
    """Apply proper English capitalization rules"""
    if not sentence:
        return sentence
    
    result = []
    capitalize_next = True
    
    words = sentence.split()
    
    for word in words:
        if capitalize_next and word:
            word = word[0].upper() + word[1:] if len(word) > 1 else word.upper()
            capitalize_next = False
        
        if word.lower() == 'i':
            word = 'I'
        
        if word and word[-1] in '.!?':
            capitalize_next = True
        
        result.append(word)
    
    return ' '.join(result)

def clean_reconstructed_sentence(sentence):
    """
    Cleanup pass for spacing and tokenization artifacts before rendering.
    """
    if not sentence:
        return sentence

    cleaned = sentence
    cleaned = cleaned.replace('##', '')
    cleaned = cleaned.replace('▁', '')
    cleaned = cleaned.replace('Ġ', '')
    cleaned = cleaned.replace('â–', '')
    cleaned = cleaned.replace('Ä ', '')
    cleaned = re.sub(r'\b([A-Za-z]{3,})\s+(ed|ing|ly)\b', r'\1\2', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+([,.!?;:])', r'\1', cleaned)
    cleaned = re.sub(r'([(\[{])\s+', r'\1', cleaned)
    cleaned = re.sub(r'\s+([)\]}])', r'\1', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    return cleaned

#sentence reconstruction using top ranked choices
def reconstruct_sentence(sentence, predictions_data, mode):
    """Reconstruct sentence with best predictions"""
    if mode != 'top':
        return None
    
    result = sentence
    
    if 'missing_letters' in predictions_data:
        for item in predictions_data['missing_letters']:
            pattern = item['pattern']
            if item['predictions'] and len(item['predictions']) > 0:
                if 'word' in item['predictions'][0] and item['predictions'][0]['word'] != 'No match found':
                    best_word = item['predictions'][0]['word']
                    result = result.replace(pattern, best_word, 1)
    
    if 'missing_words' in predictions_data and predictions_data['missing_words']:
        if isinstance(predictions_data['missing_words'], list) and len(predictions_data['missing_words']) > 0:
            if 'position' in predictions_data['missing_words'][0]:
                for blank_data in predictions_data['missing_words']:
                    if blank_data['predictions'] and len(blank_data['predictions']) > 0:
                        if 'word' in blank_data['predictions'][0]:
                            best_word = blank_data['predictions'][0]['word']
                            result = result.replace('___', best_word, 1)
            else:
                if 'word' in predictions_data['missing_words'][0]:
                    best_word = predictions_data['missing_words'][0]['word']
                    result = result.replace('___', best_word, 1)
    
    if 'predictions' in predictions_data and predictions_data['predictions']:
        if predictions_data['type'] == 'missing_words':
            if isinstance(predictions_data['predictions'], list) and len(predictions_data['predictions']) > 0:
                if 'position' in predictions_data['predictions'][0]:
                    for blank_data in predictions_data['predictions']:
                        if blank_data['predictions'] and len(blank_data['predictions']) > 0:
                            if 'word' in blank_data['predictions'][0]:
                                best_word = blank_data['predictions'][0]['word']
                                result = result.replace('___', best_word, 1)
                elif 'word' in predictions_data['predictions'][0]:
                    best_word = predictions_data['predictions'][0]['word']
                    result = result.replace('___', best_word, 1)
        
        elif predictions_data['type'] == 'missing_letters':
            for item in predictions_data['predictions']:
                pattern = item['pattern']
                if item['predictions'] and len(item['predictions']) > 0:
                    if 'word' in item['predictions'][0] and item['predictions'][0]['word'] != 'No match found':
                        best_word = item['predictions'][0]['word']
                        result = result.replace(pattern, best_word, 1)
    
    if result and result != sentence:
        result = clean_reconstructed_sentence(result)
        result = capitalize_sentence(result)
    
    return result if result != sentence else None

#pipeline for processing 
def process_sentence(sentence, dictionary, mode='top'):
    """Main processing function"""
    mode = normalize_mode_value(mode)
    # Base response envelope returned to PHP/frontend. keeps a stable schema regardless of whether input has missing words, missing letters or both
    results = {
        'input': sentence,
        'type': None,
        'output_mode': mode,
        'system': {
            'word_prediction': f'BERT AI (Transformer: {MODEL_LABEL})',
            'model_name': MODEL_LABEL,
            'model_source': MODEL_SOURCE,
            'letter_completion': 'Dictionary Pattern Matching + AI Ranking',
            'dictionary_size': len(dictionary)
        },
        'predictions': None
    }
    
    # Detect placeholder types:
    # ___ is missing words
    # fi_e mean missing letters
    has_missing_words = '___' in sentence
    debug_log(f"DEBUG process_sentence: Calling find_words_with_missing_letters on: '{sentence}'", file=sys.stderr)
    words_with_gaps = find_words_with_missing_letters(sentence)
    debug_log(f"DEBUG process_sentence: Found {len(words_with_gaps)} words with gaps", file=sys.stderr)
    has_missing_letters = len(words_with_gaps) > 0
    
    if has_missing_words and has_missing_letters:
        # Mixed mode: run a two-pass strategy for better context quality
        # Pass 1 solves letter-gaps; Pass 2 solves word blanks using updated context
        results['type'] = 'mixed'
        mixed_runtime = build_mixed_runtime_options(sentence, words_with_gaps, mode)
        if mixed_runtime['is_heavy']:
            results['system']['performance_profile'] = 'mixed-top3-optimized'
        
        letter_results = []
        # This sentence evolves as we fill best letter candidates, helping word prediction
        context_sentence = sentence
        
        debug_log(f"DEBUG: PASS 1 - Resolving missing letters in: '{sentence}'", file=sys.stderr)
        
        for word_info in words_with_gaps:
            # Predict top candidates for each letter-gap token (dictionary + model ranking)
            letter_predictions = predict_missing_letters(
                sentence,
                word_info,
                dictionary,
                fast_mode=mixed_runtime['letter_fast_mode']
            )
            letter_results.append({
                'pattern': word_info['pattern'],
                'predictions': letter_predictions
            })
            
            # Greedy contextual replacement with the top candidate only
            # This is used only to improve downstream missing-word inference context
            if letter_predictions and len(letter_predictions) > 0:
                if 'word' in letter_predictions[0] and letter_predictions[0]['word'] != 'No match found':
                    best_word = letter_predictions[0]['word']
                    context_sentence = context_sentence.replace(word_info['pattern'], best_word, 1)
                    debug_log(f"DEBUG: Replaced '{word_info['pattern']}' with '{best_word}'", file=sys.stderr)
        
        debug_log(f"DEBUG: PASS 2 - Context sentence for word prediction: '{context_sentence}'", file=sys.stderr)
        
        # Predict missing whole words after context has been improved by letter replacements
        word_preds = predict_missing_words(context_sentence, mode=mode, runtime_options=mixed_runtime)
        
        debug_log(f"DEBUG: word_preds from predict_missing_words: {word_preds}", file=sys.stderr)
        
        # Store mixed outputs in separate arrays so frontend can render two sections:
        # missing_letters and missing_words
        # limit_predictions() enforces best/top3 mode at output stage
        results['missing_letters'] = [
            {
                'pattern': item['pattern'],
                'predictions': limit_predictions(item['predictions'], mode)
            }
            for item in letter_results
        ]
        results['missing_words'] = limit_predictions(word_preds, mode)
        
        debug_log(f"DEBUG: results['missing_words'] after limit_predictions: {results['missing_words']}", file=sys.stderr)
    
    elif has_missing_words:
        # Word-only path: no letter-gap preprocessing needed
        results['type'] = 'missing_words'
        word_preds = predict_missing_words(sentence, mode=mode)
        results['predictions'] = limit_predictions(word_preds, mode)
    
    elif has_missing_letters:
        # Letter-only path: process each gap token independently
        results['type'] = 'missing_letters'
        results['predictions'] = []
        
        for word_info in words_with_gaps:
            letter_predictions = predict_missing_letters(sentence, word_info, dictionary)
            results['predictions'].append({
                'pattern': word_info['pattern'],
                'predictions': limit_predictions(letter_predictions, mode)
            })
    
    else:
        # No placeholders detected; return a complete status to avoid unnecessary inference
        results['type'] = 'complete'
        results['note'] = 'No missing words or letters detected'
    
    # Reconstructed sentence is intentionally returned only for "best" mode
    # For top3, multiple candidates are shown and a single reconstruction is ambiguous
    if mode == 'top':
        reconstructed = reconstruct_sentence(sentence, results, mode)
        if reconstructed and reconstructed != sentence:
            results['reconstructed_sentence'] = reconstructed
    
    return results

def parse_arguments(args):
    """Parse command line arguments"""
    sentence = None
    dict_path = os.path.join(SCRIPT_DIR, "english_dictionary.txt")
    mode = "top"
    
    i = 1
    while i < len(args):
        arg = args[i]
        
        if arg.startswith('--mode='):
            mode_value = arg.split('=', 1)[1].lower()
            mode = normalize_mode_value(mode_value)
        elif arg.startswith('--'):
            pass
        elif sentence is None:
            sentence = arg
        else:
            dict_path = arg
        
        i += 1
    
    return sentence, dict_path, mode

#main method
#strict JSON on stdout
def main():
    """Main entry point"""
    
    if len(sys.argv) < 2:
        print(json.dumps({
            'error': 'No sentence provided',
            'usage': 'python predictor.py "Your sentence" [dictionary.txt] [--mode=top|top3]',
        }, indent=2))
        sys.exit(1)
    
    sentence, dict_path, mode = parse_arguments(sys.argv)
    
    if not sentence:
        print(json.dumps({'error': 'No sentence provided'}))
        sys.exit(1)
    
    dictionary = load_dictionary(dict_path)
    
    print(f"System ready (mode: {mode})", file=sys.stderr)
    print("", file=sys.stderr)
    
    results = process_sentence(sentence, dictionary, mode)
    
    print(json.dumps(results, indent=2))

if __name__ == "__main__":
    main()


