"""
Orchestration Layer for Chapter Topic Weight Calculation
Coordinates data flow between Chunks, TOC, and Questions endpoints,
and Ollama's /v1/systemone API for Nimble-based decision passes.
"""

import argparse
import asyncio
import json
import math
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple, Union

import httpx

# ----------------- CONFIGURATION DEFAULTS -----------------
def _load_env_file():
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for env_file in candidates:
        if os.path.isfile(env_file):
            try:
                with open(env_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip("'\"")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass

_load_env_file()
DEFAULT_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
DEFAULT_CHUNKS_ENDPOINT = os.getenv("CHUNKS_ENDPOINT", "http://localhost:8000/api/chunks")
DEFAULT_TOC_ENDPOINT = os.getenv("TOC_ENDPOINT", "http://localhost:8000/api/toc")
DEFAULT_QUESTIONS_ENDPOINT = os.getenv("QUESTIONS_ENDPOINT", "http://localhost:8000/api/questions")
DEFAULT_OLLAMA_SYSTEMONE_URL = os.getenv("OLLAMA_SYSTEMONE_URL", "http://localhost:11434/v1/systemone")

# Fallback port 3000 endpoints if port 8000 is not reachable
FALLBACK_PORT_3000_MAP = {
    "http://localhost:8000/api/chunks": "http://localhost:3000/api/chunks",
    "http://localhost:8000/api/toc": "http://localhost:3000/api/toc",
    "http://localhost:8000/api/questions": "http://localhost:3000/api/questions",
}

# Cognitive depth impact weights
TYPE_WEIGHTS: Dict[str, float] = {
    "factual": 1.0,
    "conceptual": 1.5,
    "numerical": 2.0,
    "derivation": 2.5,
}

PRIOR_WEIGHT: float = 0.5  # Guarantees baseline coverage for topics with 0 extracted questions

# Efficiency & Circuit Breaker Configurations
FAST_CONNECT_TIMEOUT: float = 0.35
_OFFLINE_HOSTS: set = set()
_INGESTION_CACHE: Dict[str, Any] = {}


def clear_ingestion_cache():
    """Clears in-memory ingestion cache and offline hosts list (useful between test runs)."""
    _INGESTION_CACHE.clear()
    _OFFLINE_HOSTS.clear()



def get_canonical_type(raw_type: Any, is_derivation: bool = False, is_numerical: bool = False) -> str:
    """Normalizes any question type string or synonym into one of the canonical 4 types."""
    if is_derivation:
        return "derivation"
    if is_numerical:
        return "numerical"
    t_clean = str(raw_type or "").strip().lower()
    mapping = {
        "factual": "factual",
        "recall": "factual",
        "definition": "factual",
        "direct": "factual",
        "mcq": "factual",
        "fill": "factual",
        "fill-in-the-blank": "factual",
        "tf": "factual",
        "true-false": "factual",
        "match": "factual",
        "objective": "factual",
        "conceptual": "conceptual",
        "qualitative": "conceptual",
        "understanding": "conceptual",
        "reasoning": "conceptual",
        "short": "conceptual",
        "long": "conceptual",
        "case": "conceptual",
        "odd": "conceptual",
        "numerical": "numerical",
        "calculation": "numerical",
        "computational": "numerical",
        "derivation": "derivation",
        "proof": "derivation",
        "derive": "derivation",
    }
    if t_clean in mapping:
        return mapping[t_clean]
    for key in ("derivation", "numerical", "factual", "conceptual"):
        if key in t_clean:
            return key
    return "conceptual"


def get_type_weight(raw_type: Any, is_derivation: bool = False, is_numerical: bool = False) -> float:
    """Returns the numeric cognitive depth impact weight for a question type."""
    canonical = get_canonical_type(raw_type, is_derivation=is_derivation, is_numerical=is_numerical)
    return TYPE_WEIGHTS.get(canonical, 1.5)


# ----------------- CONTENT EXTRACTION & ALIGNMENT HELPERS -----------------
def chunk_latex_document(latex_text: str) -> List[Dict[str, Any]]:
    """
    Splits a LaTeX textbook document (such as solutions_textbook_1to1.tex or standard LaTeX)
    into structured, topic-aligned chunk dictionaries.
    """
    if not latex_text or not str(latex_text).strip():
        return []

    lines = str(latex_text).splitlines(keepends=True)
    headings: List[Tuple[int, str, str]] = []  # (line_index, number, title)

    for i, line in enumerate(lines):
        # 1. Standard LaTeX commands: \section{...}, \subsection{...}, etc.
        m_cmd = re.search(r"\\(?:chapter|section|subsection|subsubsection)\*?\s*\{([^}]+)\}", line, re.IGNORECASE)
        if m_cmd:
            raw_title = m_cmd.group(1).strip()
            clean_title = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", raw_title)
            clean_title = re.sub(r"\\[a-zA-Z]+", "", clean_title).strip()
            num_m = re.match(r"^(\d+\.\d+(?:\.\d+)?)\s*(.*)", clean_title)
            if num_m:
                headings.append((i, num_m.group(1).strip(), num_m.group(2).strip() or clean_title))
            else:
                headings.append((i, "", clean_title))
            continue

        # 2. Tabular section headers: {\large\itshape 1.X} & {\large\itshape Title}
        m_tab = re.search(r"\{\\large\\itshape\s*(\d+\.\d+)\}\s*&\s*\{\\large\\itshape\s*([^}]+)\}", line)
        if m_tab:
            num = m_tab.group(1).strip()
            title = m_tab.group(2).replace(r"\\", " ").strip()
            headings.append((i, num, " ".join(title.split())))
            continue

        # 3. Raggedleft / large italic major sections: \large\itshape 1.X ...
        m1 = re.search(r"\\large\\itshape\s*(\d+\.\d+)(?:\\hspace[^}]*)?\s*&?\s*(?:\{\\large\\itshape\s*)?([^\n\\]*)", line)
        if m1 and not re.search(r"Example|Table|Fig", line, re.IGNORECASE):
            num = m1.group(1).strip()
            combined = line
            for j in range(1, 4):
                if i + j < len(lines):
                    combined += " " + lines[i+j]
            m_sub = re.search(r"\\large\\itshape\s*\d+\.\d+(?:\\hspace[^\}]*)?\s*(.*?)\}\\\\?\[", combined, re.DOTALL)
            title_part = m_sub.group(1).replace(r"\\", " ").strip() if m_sub else m1.group(2).replace(r"\\", " ").strip(" }")
            title_clean = re.sub(r"\\[a-zA-Z]+(?:\{[^}]*\})?", "", title_part).strip(" }{")
            clean_num_title = " ".join(title_clean.split())
            if clean_num_title:
                headings.append((i, num, clean_num_title))
            continue

        # 4. Subsections: \textbf{\color{gray}1.X.Y} & \textbf{...} or \textbf{1.X.Y} & \textbf{...}
        m2 = re.search(r"\\textbf\{(?:\\color\{[a-zA-Z0-9]+\})?(\d+\.\d+\.\d+)\}\s*&?\s*\\textbf\{([^}]+)\}", line)
        if m2:
            num = m2.group(1).strip()
            raw_title = m2.group(2).strip()
            clean_title = re.sub(r"\\[a-zA-Z]+(?:\{[^}]*\})?", "", raw_title).strip(" }{")
            headings.append((i, num, " ".join(clean_title.split())))
            continue

    if not headings:
        return [{"text": latex_text.strip()}]

    headings.sort(key=lambda x: x[0])
    end_line_idx = len(lines)
    for idx in range(headings[-1][0], len(lines)):
        if re.search(r"\\(?:large|textbf|section)\s*.*?(?:Summary|Exercises|Chapter Exercises)", lines[idx], re.IGNORECASE) or lines[idx].strip().startswith("Summary"):
            end_line_idx = idx
            break

    chunks: List[Dict[str, Any]] = []
    for idx in range(len(headings)):
        start_idx, num, title = headings[idx]
        next_idx = headings[idx + 1][0] if idx + 1 < len(headings) else end_line_idx
        chunk_body = "".join(lines[start_idx:next_idx]).strip()
        topic_name = f"{num} {title}".strip() if num else title
        chunks.append({
            "number": num,
            "title": title,
            "topic": topic_name,
            "text": chunk_body,
        })
    return chunks


def normalize_chunks(data: Any) -> List[Dict[str, Any]]:
    """Normalizes various JSON chunks endpoint payload formats or raw LaTeX documents into a list of dicts."""
    if isinstance(data, str):
        # If input is a raw LaTeX document string
        if r"\documentclass" in data or r"\section" in data or r"\large\itshape" in data or r"\textbf" in data:
            chunked = chunk_latex_document(data)
            if chunked:
                return chunked
        return [{"text": data.strip()}]

    raw_list: List[Any] = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("chunks", "data", "topics", "items", "content"):
            if key in data and isinstance(data[key], list):
                raw_list = data[key]
                break

    result: List[Dict[str, Any]] = []
    for item in raw_list:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            result.append({"text": item.strip()})
        elif item is not None:
            result.append({"text": str(item).strip()})
    return result


def normalize_toc_topics(data: Any) -> List[str]:
    """Normalizes TOC endpoint payload formats into a list of topic name strings."""
    if not data:
        return []

    # 1. Plain list of strings or dicts
    if isinstance(data, list):
        # Check if list contains dicts with hierarchical children
        has_children = any(isinstance(item, dict) and "children" in item for item in data)
        if has_children:
            topics: List[str] = []
            def _traverse(node: Dict[str, Any], is_root: bool = False):
                title = str(node.get("title") or node.get("name") or node.get("topic") or "").strip()
                is_meta = any(m in title.lower() for m in ["intext question", "summary", "exercises", "answers to"])
                if not is_root and not is_meta and title:
                    topics.append(title)
                for child in node.get("children", []):
                    if isinstance(child, dict):
                        _traverse(child, is_root=False)

            for item in data:
                if isinstance(item, dict):
                    is_chapter_root = bool(item.get("children")) and item.get("level", 1) == 1
                    _traverse(item, is_root=is_chapter_root)
                elif item:
                    topics.append(str(item).strip())
            return [t for t in topics if t]

        result: List[str] = []
        for item in data:
            if isinstance(item, dict):
                num = str(item.get("number", "")).strip()
                title = str(item.get("title") or item.get("name") or item.get("topic") or "").strip()
                if num and title:
                    result.append(f"{num} {title}")
                elif title:
                    result.append(title)
                else:
                    result.append(str(item).strip())
            elif item:
                result.append(str(item).strip())
        return [r for r in result if r]

    if isinstance(data, dict):
        if "toc" in data:
            if isinstance(data["toc"], dict):
                return normalize_toc_topics(data["toc"])
            elif isinstance(data["toc"], list):
                return normalize_toc_topics(data["toc"])
        for key in ("topics", "toc", "subtopics", "data", "items"):
            if key in data and isinstance(data[key], list):
                return normalize_toc_topics(data[key])
    return []


def format_question_text(item: Any) -> str:
    """Formats a question item into a self-contained question string."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        q = (
            item.get("q")
            or item.get("text")
            or item.get("latex")
            or item.get("question")
            or item.get("content")
        )
        if q:
            text = str(q).strip()
            passage = item.get("passage")
            if passage:
                text = f"{str(passage).strip()}\n{text}"
            opts = item.get("opts")
            if opts and isinstance(opts, list):
                text = f"{text}\n" + "\n".join(str(o) for o in opts)
            return text
        return str(item).strip()
    return str(item or "").strip()


def normalize_questions(data: Any) -> List[str]:
    """Normalizes questions endpoint payload formats into a list of question strings."""
    raw_list: List[Any] = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("questions", "data", "items", "extracted_questions"):
            if key in data and isinstance(data[key], list):
                raw_list = data[key]
                break

    result: List[str] = []
    for item in raw_list:
        txt = format_question_text(item)
        if txt:
            result.append(txt)
    return result


def normalize_question_records(data: Any) -> List[Dict[str, Any]]:
    """Extracts rich question records with annotated topic, type, and difficulty metadata."""
    raw_list: List[Any] = []
    if isinstance(data, list):
        raw_list = data
    elif isinstance(data, dict):
        for key in ("questions", "data", "items", "extracted_questions"):
            if key in data and isinstance(data[key], list):
                raw_list = data[key]
                break

    records: List[Dict[str, Any]] = []
    for item in raw_list:
        if isinstance(item, dict):
            txt = format_question_text(item)
            records.append({
                "text": txt,
                "topic": item.get("topic") or item.get("assigned_topic"),
                "type": item.get("type"),
                "isDerivation": bool(item.get("isDerivation")),
                "isNumerical": bool(item.get("isNumerical")),
                "tier": item.get("tier"),
                "marks": item.get("marks"),
                "raw": item,
            })
        elif isinstance(item, str):
            records.append({
                "text": item.strip(),
                "topic": None,
                "type": None,
                "isDerivation": False,
                "isNumerical": False,
                "tier": None,
                "marks": None,
                "raw": item,
            })
    return records


def extract_chunk_text(chunk: Any) -> str:
    """Safely extracts LaTeX or plain text from a chunk dictionary or string."""
    if isinstance(chunk, str):
        return chunk.strip()
    if isinstance(chunk, dict):
        text = chunk.get("text") or chunk.get("content") or chunk.get("latex") or chunk.get("raw") or ""
        return str(text).strip()
    return str(chunk or "").strip()


def extract_latex_headers(text: str) -> List[str]:
    r"""
    Extracts LaTeX section, subsection, subsubsection, chapter, and paragraph titles
    using balanced brace parsing to correctly handle nested commands like \textbf{}, \label{}, etc.
    Also extracts tabular and large/itshape textbook headings.
    """
    results: List[str] = []

    # 1. Standard commands
    cmd_pattern = re.compile(
        r"\\(?:chapter|section|subsection|subsubsection|paragraph)\*?\s*\{",
        re.IGNORECASE,
    )
    for m in cmd_pattern.finditer(text):
        start = m.end()
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1

        if depth == 0:
            raw_title = text[start : i - 1]
            clean = re.sub(r"\\label\{[^}]*\}", "", raw_title)
            clean = re.sub(r"\\[a-zA-Z]+\{([^}]*)\}", r"\1", clean)
            clean = re.sub(r"\\[a-zA-Z]+", "", clean)
            clean = " ".join(clean.split()).strip()
            if clean:
                results.append(clean)

    # 2. Tabular section headers: {\large\itshape 1.X} & {\large\itshape Title}
    for m in re.finditer(r"\{\\large\\itshape\s*(\d+\.\d+)\}\s*&\s*\{\\large\\itshape\s*([^}]+)\}", text):
        clean = " ".join(m.group(2).replace(r"\\", " ").split()).strip()
        if clean:
            results.append(f"{m.group(1)} {clean}")

    # 3. Subsection headers: \textbf{\color{gray}1.X.Y} & \textbf{Title}
    for m in re.finditer(r"\\textbf\{(?:\\color\{[a-zA-Z0-9]+\})?(\d+\.\d+\.\d+)\}\s*&?\s*\\textbf\{([^}]+)\}", text):
        clean = " ".join(m.group(2).replace(r"\\", " ").split()).strip()
        if clean:
            results.append(f"{m.group(1)} {clean}")

    return results


def align_chunks_to_topics(chunks: List[Any], toc_topics: List[str]) -> Dict[str, str]:
    r"""
    Groups raw LaTeX chunk texts by their corresponding TOC topic.
    1. Direct match on chunk 'number', 'topic', or 'title' metadata.
    2. Matching LaTeX section/subsection headers against TOC titles.
    3. Case-insensitive substring match in the chunk header text snippet.
    4. Fast pre-tokenized keyword match across chunk content.
    """
    topic_texts: Dict[str, str] = defaultdict(str)
    topic_lower_map = {t.strip().lower(): t for t in toc_topics}

    def strip_numbering(s: str) -> str:
        return re.sub(r"^\s*(?:\d+\.)*\d+\s*", "", s).strip()

    # Pre-tokenize topics for fast keyword matching
    topic_tokens = {
        t: [w for w in re.split(r"\W+", t.lower()) if len(w) > 3]
        for t in toc_topics
    }

    for chunk in chunks:
        chunk_text = extract_chunk_text(chunk)
        chunk_num = str(chunk.get("number") or "").strip() if isinstance(chunk, dict) else ""
        chunk_topic = chunk.get("topic") or chunk.get("title") if isinstance(chunk, dict) else None

        # 1. Match by chunk number prefix (e.g. "1.1", "1.3.1")
        assigned = False
        if chunk_num:
            for t in toc_topics:
                if re.match(r"^" + re.escape(chunk_num) + r"(?:\s|:|-|$)", t):
                    topic_texts[t] += "\n" + chunk_text
                    assigned = True
                    break
        if assigned:
            continue

        # 2. Direct metadata match
        if chunk_topic:
            chunk_topic_str = str(chunk_topic).strip()
            if chunk_topic_str in toc_topics:
                topic_texts[chunk_topic_str] += "\n" + chunk_text
                continue
            if chunk_topic_str.lower() in topic_lower_map:
                topic_texts[topic_lower_map[chunk_topic_str.lower()]] += "\n" + chunk_text
                continue
            unnum = strip_numbering(chunk_topic_str).lower()
            if unnum in topic_lower_map:
                topic_texts[topic_lower_map[unnum]] += "\n" + chunk_text
                continue

        # 3. Extract LaTeX headers and match with TOC
        headers = extract_latex_headers(chunk_text)
        for header in headers:
            header_clean = header.strip()
            header_lower = header_clean.lower()
            header_unnum = strip_numbering(header_clean).lower()

            if header_clean in toc_topics:
                topic_texts[header_clean] += "\n" + chunk_text
                assigned = True
                break
            if header_lower in topic_lower_map:
                topic_texts[topic_lower_map[header_lower]] += "\n" + chunk_text
                assigned = True
                break
            if header_unnum in topic_lower_map:
                topic_texts[topic_lower_map[header_unnum]] += "\n" + chunk_text
                assigned = True
                break

            # Number boundary match on header
            h_num_m = re.match(r"^(\d+\.\d+(?:\.\d+)?)\s*", header_clean)
            if h_num_m:
                h_num = h_num_m.group(1)
                for t in toc_topics:
                    if re.match(r"^" + re.escape(h_num) + r"(?:\s|:|-|$)", t):
                        topic_texts[t] += "\n" + chunk_text
                        assigned = True
                        break
                if assigned:
                    break

            # Substring match on header
            for t in toc_topics:
                t_low = t.lower()
                if t_low in header_lower or header_lower in t_low or t_low in header_unnum or header_unnum in t_low:
                    topic_texts[t] += "\n" + chunk_text
                    assigned = True
                    break
            if assigned:
                break

        if assigned:
            continue

        # 4. Fallback: match TOC topics in the initial 300 characters of chunk text
        prefix = chunk_text.lower()[:300]
        for t in toc_topics:
            t_low = t.lower()
            if t_low in prefix or strip_numbering(t).lower() in prefix:
                topic_texts[t] += "\n" + chunk_text
                assigned = True
                break

        # 5. Fallback: fast pre-tokenized keyword match across chunk text
        if not assigned and toc_topics:
            best_t = None
            max_hits = 0
            text_lower = chunk_text.lower()
            for t, words in topic_tokens.items():
                hits = sum(text_lower.count(w) for w in words)
                if hits > max_hits:
                    max_hits = hits
                    best_t = t
            if best_t and max_hits > 0:
                topic_texts[best_t] += "\n" + chunk_text

    # Hierarchical coalescence: coalesce child subtopics into parent topic chunks
    # so container headings have full theoretical and mathematical substance.
    topic_num_map = {}
    for t in toc_topics:
        m = re.match(r"^(\d+(?:\.\d+)+)\b", t.strip())
        if m:
            topic_num_map[t] = m.group(1)

    for p_topic, p_num in topic_num_map.items():
        child_texts = []
        for c_topic, c_num in topic_num_map.items():
            if c_topic != p_topic and c_num.startswith(p_num + "."):
                c_text = topic_texts.get(c_topic, "").strip()
                if c_text and c_text not in topic_texts[p_topic]:
                    child_texts.append(c_text)
        if child_texts:
            topic_texts[p_topic] = (topic_texts.get(p_topic, "").strip() + "\n\n" + "\n\n".join(child_texts)).strip()

    return topic_texts


def select_candidate_topics(question_latex: str, toc_topics: List[str], max_options: int = 26) -> List[str]:
    """
    Selects up to max_options (26) candidate TOC topics for Nimble's choice question.
    If len(toc_topics) <= 26, preserves all topics in order.
    If len(toc_topics) > 26, ranks candidate topics by keyword relevance to the question
    so relevant topics beyond index 25 are not erroneously discarded.
    """
    if len(toc_topics) <= max_options:
        return list(toc_topics)

    q_lower = str(question_latex or "").lower()
    scored_topics: List[Tuple[int, int, str]] = []

    for idx, topic in enumerate(toc_topics):
        t_low = topic.lower()
        score = 0
        if t_low in q_lower:
            score += 100
        words = [w for w in re.split(r"\W+", t_low) if len(w) > 3]
        for w in words:
            if w in q_lower:
                score += 10
        # Tuple: (score, negative idx so earlier topics are favored on ties, topic)
        scored_topics.append((score, -idx, topic))

    # Sort descending by score, then by original order
    scored_topics.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top_candidates = [item[2] for item in scored_topics[:max_options]]

    # Re-order the selected candidates back to their original TOC sequence
    toc_indices = {t: i for i, t in enumerate(toc_topics)}
    top_candidates.sort(key=lambda t: toc_indices.get(t, 9999))
    return top_candidates


# ----------------- MOCK / HEURISTIC DECISION ENGINE -----------------
def mock_classify_question(question_latex: Any, toc_topics: List[str]) -> Dict[str, str]:
    """Heuristic offline classifier for questions when Ollama is unavailable."""
    # Check if question is a rich dict
    if isinstance(question_latex, dict):
        raw_top = str(question_latex.get("topic") or "").strip()
        if raw_top:
            for ct in toc_topics:
                if ct.lower() == raw_top.lower() or re.match(r"^" + re.escape(raw_top) + r"(?:\s|:|-|$)", ct):
                    q_type = get_canonical_type(
                        question_latex.get("type"),
                        is_derivation=question_latex.get("isDerivation", False),
                        is_numerical=question_latex.get("isNumerical", False),
                    )
                    return {"topic": ct, "type": q_type}
        q_str = format_question_text(question_latex)
        q_type_hint = question_latex.get("type")
        is_deriv = question_latex.get("isDerivation", False)
        is_num = question_latex.get("isNumerical", False)
    else:
        q_str = str(question_latex or "")
        q_type_hint = None
        is_deriv = False
        is_num = False

    q_lower = q_str.lower()

    # Topic-specific contextual associations for educational topics across STEM subjects
    topic_keywords = {
        "faraday": ["faraday", "flux", "emf", "electromotive", "n turns", "rotating coil", "rate of change"],
        "lenz": ["lenz", "direction of induced", "oppose", "conservation of energy", "polarity", "magnet falls", "approaches"],
        "eddy": ["eddy", "damping", "galvanometer", "dead-beat", "copper plate", "pendulum", "laminat", "swirling", "foucault"],
        "types of solutions": ["amalgam", "camphor", "types of solution", "binary solution", "solute and solvent", "table 1.1", "gaseous solution", "solid solution"],
        "expressing concentration": ["molarity", "molality", "mole fraction", "ppm", "parts per million", "mass percentage", "temperature independent"],
        "solubility of a solid": ["solid in liquid", "solid in a liquid", "dissolution", "crystallisation", "saturated solution", "le chatelier"],
        "solubility of a gas": ["gas in liquid", "gas in a liquid", "henry", "henry's law", "k_h", "aquatic species", "anoxia", "bends", "scuba", "soft drinks"],
        "vapour pressure of liquid-liquid": ["liquid-liquid", "liquid liquid", "raoult", "volatile liquids", "partial vapour pressure", "dalton"],
        "special case of henry": ["special case of henry", "raoult as a special case", "raoult's law as a special case"],
        "solutions of solids in liquids": ["solutions of solids in liquids", "non-volatile solute", "decrease in vapour pressure"],
        "ideal solutions": ["ideal solution", "ideal solutions", "delta_mix h = 0", "delta_mix v = 0", "benzene and toluene", "hexane and heptane"],
        "non-ideal solutions": ["non-ideal", "positive deviation", "negative deviation", "azeotrope", "azeotropic", "minimum boiling", "maximum boiling", "ethanol and acetone", "chloroform and acetone"],
        "relative lowering": ["relative lowering", "relative lowering of vapour", "p_1^0", "p1^0", "lowering of vapour pressure"],
        "elevation of boiling": ["elevation of boiling", "boiling point elevation", "ebullioscopic", "k_b", "molal elevation", "delta t_b"],
        "depression of freezing": ["depression of freezing", "freezing point depression", "cryoscopic", "k_f", "molal depression", "delta t_f", "antifreeze", "ethylene glycol"],
        "osmosis and osmotic": ["osmosis", "osmotic pressure", "semipermeable", "semi-permeable", "isotonic", "hypertonic", "hypotonic", "molar mass of polymers", "pi = crt", "blood cells"],
        "reverse osmosis": ["reverse osmosis", "water purification", "desalination", "cellulose acetate"],
        "abnormal molar": ["abnormal molar", "van 't hoff", "van’t hoff", "van t hoff", "dissociation", "association", "factor i", "degree of dissociation", "dimerisation", "dimer"],
    }

    best_topic = toc_topics[0] if toc_topics else "General"
    max_score = -1

    candidates = select_candidate_topics(q_str, toc_topics, 26)

    for topic in candidates:
        score = 0
        topic_lower = topic.lower()

        # Check explicit topic number in question
        num_m = re.match(r"^(\d+\.\d+(?:\.\d+)*)", topic)
        if num_m:
            t_num = num_m.group(1)
            if re.search(r"\b" + re.escape(t_num) + r"\b", q_lower):
                score += 50
            # Deeper topics get slight specificity preference on ties (e.g. 1.6.3 over 1.6)
            score += t_num.count(".") * 2

        # Direct name match
        title_only = re.sub(r"^\d+\.\d+(?:\.\d+)*\s*", "", topic_lower).strip()
        if title_only and title_only in q_lower:
            score += 30
        elif topic_lower in q_lower:
            score += 25

        # Association lookup
        for key, kws in topic_keywords.items():
            if key in topic_lower or (title_only and key in title_only):
                for kw in kws:
                    if kw in q_lower:
                        score += 15

        # Word overlap
        for word in re.split(r"\W+", topic_lower):
            if len(word) > 3 and word not in ("induction", "currents", "law", "chapter", "solution", "solutions", "liquid"):
                if word in q_lower:
                    score += 4

        if score > max_score:
            max_score = score
            best_topic = topic

    # Determine question cognitive format
    if is_deriv or any(k in q_lower for k in ("derive", "derivation", "first principles", "proof", "prove")):
        q_type = "derivation"
    elif is_num or any(k in q_lower for k in ("calculate", "numerical", "find the value", "magnitude of", "mass of", "mole fraction")) or re.search(
        r"\d+\.?\d*\s*(t/s|cm|m|v|a|wb|ohm|rad|g|kg|mol|bar|torr|kpa|atm|mmhg|pm|nm)", q_lower
    ):
        q_type = "numerical"
    elif any(k in q_lower for k in ("explain", "why", "consequence", "predict", "plot", "reason")):
        q_type = "conceptual"
    elif q_type_hint and get_canonical_type(q_type_hint) != "conceptual":
        q_type = get_canonical_type(q_type_hint)
    elif any(k in q_lower for k in ("state", "define", "what are", "what is", "unit", "formula", "mention", "which of")):
        q_type = "factual"
    else:
        q_type = "conceptual"

    return {"topic": best_topic, "type": q_type}


def mock_score_topic_difficulty(topic_name: str, topic_latex_text: str) -> float:
    """Heuristic offline difficulty scorer for topic LaTeX content."""
    t_name = str(topic_name or "").lower()
    text = str(topic_latex_text or "").lower()

    if "faraday" in t_name:
        return 1.85
    elif "lenz" in t_name:
        return 1.25
    elif "eddy" in t_name:
        return 1.40

    is_high_rigor = (
        any(k in text for k in (r"\frac{d", r"\partial", r"\int", r"\mathbf", "derivative", "calculus", "first principles"))
        or any(k in text for k in (r"k_b", r"k_f", r"k_h", r"van 't hoff", r"van’t hoff", "abnormal molar", r"\delta t_b", r"\delta t_f", r"\pi = crt"))
        or "abnormal molar" in t_name
        or "freezing point" in t_name
        or "boiling point" in t_name
        or "osmotic" in t_name
        or "vapour pressure of liquid-liquid" in t_name
    )
    if is_high_rigor:
        bonus = 0.0
        if "abnormal" in t_name or "freezing" in t_name:
            bonus += 0.10
        if "example" in text:
            bonus += 0.05
        return round(min(2.0, 1.75 + bonus), 2)

    is_med_rigor = (
        any(k in text for k in (r"\begin{equation}", r"\tag{", "=", "formula", "proportional", "molarity", "molality", "solubility", "henry", "raoult", "azeotrope"))
        or "concentration" in t_name
        or "solubility" in t_name
        or "non-ideal" in t_name
        or "relative lowering" in t_name
        or "gas in a liquid" in t_name
    )
    if is_med_rigor:
        bonus = 0.10 if ("henry" in text or "concentration" in t_name or "gas" in t_name) else 0.0
        return round(1.40 + bonus, 2)

    if any(k in text for k in ("concept", "phenomenon", "qualitative", "principle", "types of")):
        return 1.25
    return 1.20


# ----------------- PASS 1: QUESTION CLASSIFIER -----------------
async def classify_question_with_nimble(
    client: httpx.AsyncClient,
    question_latex: str,
    toc_topics: List[str],
    nimble_url: str = DEFAULT_OLLAMA_SYSTEMONE_URL,
    model: str = "nimble",
    timeout: float = 60.0,
    mock_mode: bool = False,
) -> Dict[str, str]:
    """
    Classifies a question's parent topic and cognitive type using Ollama's /v1/systemone endpoint.
    Restricts candidate topics to at most 26 subtopics as per Ollama Nimble choice limit.
    """
    if mock_mode:
        return mock_classify_question(question_latex, toc_topics)

    candidate_topics = select_candidate_topics(question_latex, toc_topics, 26)
    topic_criteria = {topic: f"Covers {topic}" for topic in candidate_topics}

    payload = {
        "model": model,
        "state": f"Question:\n{question_latex}",
        "questions": {
            "assigned_topic": {
                "type": "choice",
                "instructions": "Which topic does this question evaluate?",
                "criteria": topic_criteria,
            },
            "question_type": {
                "type": "choice",
                "instructions": "What is the primary format and cognitive type of this question?",
                "criteria": {
                    "factual": "Basic definition or direct factual recall",
                    "conceptual": "Qualitative explanation of principles or theorems",
                    "numerical": "Mathematical calculation with values or variables",
                    "derivation": "Formal mathematical proof or formula derivation",
                },
            },
        },
    }

    try:
        res = await client.post(nimble_url, json=payload, timeout=timeout)
        res.raise_for_status()
        data = res.json()
        answers = data.get("answers", {}) if isinstance(data, dict) else {}

        raw_topic = ""
        if isinstance(answers.get("assigned_topic"), dict):
            raw_topic = answers["assigned_topic"].get("choice", "")
        elif isinstance(answers.get("assigned_topic"), str):
            raw_topic = answers["assigned_topic"]

        raw_type = ""
        if isinstance(answers.get("question_type"), dict):
            raw_type = answers["question_type"].get("choice", "")
        elif isinstance(answers.get("question_type"), str):
            raw_type = answers["question_type"]

        # Match assigned topic safely against candidate topics (case-insensitive & substring)
        raw_topic_clean = str(raw_topic).strip()
        matched_topic = None
        for ct in candidate_topics:
            if ct.lower() == raw_topic_clean.lower():
                matched_topic = ct
                break
        if not matched_topic:
            for ct in candidate_topics:
                if ct.lower() in raw_topic_clean.lower() or raw_topic_clean.lower() in ct.lower():
                    matched_topic = ct
                    break
        if not matched_topic and candidate_topics:
            matched_topic = candidate_topics[0]

        assigned_topic = matched_topic or (candidate_topics[0] if candidate_topics else "General")
        question_type = get_canonical_type(raw_type)

        return {"topic": assigned_topic, "type": question_type}

    except Exception as exc:
        is_conn_error = isinstance(exc, (httpx.RequestError, httpx.TimeoutException)) or any(
            err_term in str(exc).lower() for err_term in ("connect", "refused", "timeout", "offline")
        )
        if mock_mode or is_conn_error:
            return mock_classify_question(question_latex, toc_topics)
        raise


# ----------------- PASS 2: TOPIC RIGOR & DIFFICULTY SCORING -----------------
async def score_topic_difficulty_with_nimble(
    client: httpx.AsyncClient,
    topic_name: str,
    topic_latex_text: str,
    nimble_url: str = DEFAULT_OLLAMA_SYSTEMONE_URL,
    model: str = "nimble",
    timeout: float = 60.0,
    mock_mode: bool = False,
) -> float:
    """
    Evaluates the technical complexity of a topic's LaTeX content.
    Enforces safe context window snippet limit (2,000 - 3,500 characters).
    Maps 3-tier rubric raw score (0.0 to 2.0) to difficulty multiplier [1.0, 2.0].
    """
    if mock_mode:
        return mock_score_topic_difficulty(topic_name, topic_latex_text)

    snippet = topic_latex_text[:3500] if topic_latex_text and topic_latex_text.strip() else topic_name

    payload = {
        "model": model,
        "state": f"Topic: {topic_name}\nContent:\n{snippet}",
        "questions": {
            "technical_rigor": {
                "type": "score",
                "instructions": "Rate the technical and mathematical difficulty of this topic.",
                "criteria": [
                    "Introductory qualitative descriptions and basic terminology",
                    "Applied concepts with single-variable formulas or straightforward relations",
                    "Advanced derivations, calculus, multi-variable vector algebra, or abstract theory",
                ],
            }
        },
    }

    try:
        res = await client.post(nimble_url, json=payload, timeout=timeout)
        res.raise_for_status()
        data = res.json()
        answers = data.get("answers", {}) if isinstance(data, dict) else {}
        rigor = answers.get("technical_rigor", {})
        if isinstance(rigor, dict):
            raw_score = rigor.get("score", 1.0)
        else:
            raw_score = float(rigor) if rigor is not None else 1.0

        clamped_score = max(0.0, min(2.0, float(raw_score)))
        return 1.0 + (clamped_score / 2.0)

    except Exception as exc:
        is_conn_error = isinstance(exc, (httpx.RequestError, httpx.TimeoutException)) or any(
            err_term in str(exc).lower() for err_term in ("connect", "refused", "timeout", "offline")
        )
        if mock_mode or is_conn_error:
            return mock_score_topic_difficulty(topic_name, topic_latex_text)
        raise


# ----------------- DETERMINISTIC SYNTHESIS & APPORTIONMENT -----------------
def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely converts any input into a finite float, falling back to default."""
    if val is None:
        return default
    try:
        f = float(val)
        return default if (math.isnan(f) or math.isinf(f)) else f
    except (ValueError, TypeError):
        return default


MIN_MANUAL_QUESTIONS = 10
MAX_MANUAL_QUESTIONS = 50


def validate_target_questions(
    val: Any,
    clamp: bool = True,
) -> Tuple[Optional[int], bool]:
    """
    Validates and resolves target questions parameter for dual-mode operation:
    - Dynamic mode: returns (None, True) when val is None or in ('auto', 'dynamic', '').
    - Manual mode: returns (integer_val, False) when val is numeric in [10, 50].
      If outside [10, 50] and clamp=True, returns clamped integer in [10, 50].
      If outside [10, 50] and clamp=False, raises ValueError.
      If non-numeric and clamp=False, raises ValueError.
    """
    if isinstance(val, bool):
        return None, True
    if val is None:
        return None, True
    if isinstance(val, str):
        cleaned = val.strip().lower()
        if cleaned in ("auto", "dynamic", "none", ""):
            return None, True
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            if not clamp:
                raise ValueError(f"Manual target questions must be a finite number, got {val}")
            return None, True
        num = int(round(f))
        if num < MIN_MANUAL_QUESTIONS or num > MAX_MANUAL_QUESTIONS:
            if clamp:
                clamped = max(MIN_MANUAL_QUESTIONS, min(MAX_MANUAL_QUESTIONS, num))
                return clamped, False
            raise ValueError(
                f"Manual target questions must be between {MIN_MANUAL_QUESTIONS} and {MAX_MANUAL_QUESTIONS}, got {val}"
            )
        return num, False
    except (TypeError, ValueError) as err:
        if isinstance(err, ValueError) and "Manual target questions" in str(err):
            raise
        if not clamp:
            raise ValueError(
                f"Invalid manual target-questions value: '{val}'. Expected an integer between {MIN_MANUAL_QUESTIONS} and {MAX_MANUAL_QUESTIONS} (inclusive), or 'auto'."
            )
        return None, True


# ----------------- DETERMINISTIC SYNTHESIS & APPORTIONMENT -----------------
def compute_dynamic_target_questions(
    raw_weights_or_manifest: Union[Dict[str, Any], List[Any], Tuple[Any, ...]],
    topic_impacts: Optional[Dict[str, Any]] = None,
    difficulty_map: Optional[Dict[str, Any]] = None,
    min_questions: Optional[Union[int, float, str]] = None,
    max_questions: Optional[Union[int, float, str]] = 50,
    c1: Union[float, str] = 0.50,
    c2: Union[float, str] = 0.50,
    prior: Union[float, str] = PRIOR_WEIGHT,
) -> int:
    """
    Computes optimal chapter question limit dynamically based on:
    - Raw topic weight sum: sum(W_k)
    - Significant topic count: N_sig
    - Difficulty density: weighted average topic difficulty D_density / 1.5
    - Mathematical formula:
        Q_raw = (c1 * N_sig + c2 * sqrt(sum(W_k))) * (D_density / 1.5)
        Q_dynamic = clamp(round(Q_raw), min_questions, max_questions)
    Ensures every significant topic receives appropriate representation while calibrating
    for chapter technical rigor and cognitive depth.
    Handles corrupt inputs, malformed types, and manifest/list/dict formats defensively.
    """
    if not raw_weights_or_manifest:
        return 0

    c1_f = _safe_float(c1, 0.50)
    c2_f = _safe_float(c2, 0.50)
    prior_f = _safe_float(prior, PRIOR_WEIGHT)

    raw_w_map: Dict[str, float] = {}
    diff_map: Dict[str, float] = {}
    impacts_map: Dict[str, float] = {}

    # Case 1: Synthesized chapter manifest dict {"topics": [...]}
    if isinstance(raw_weights_or_manifest, dict) and "topics" in raw_weights_or_manifest:
        raw_topics = raw_weights_or_manifest.get("topics", [])
        if not isinstance(raw_topics, list) or not raw_topics:
            return 0
        for i, t in enumerate(raw_topics):
            if not isinstance(t, dict):
                continue
            name = str(t.get("topic_name", "") or f"Topic_{i}")
            impact = max(0.0, _safe_float(t.get("question_impact_score", t.get("existing_questions", 0.0)), 0.0))
            diff = max(0.1, _safe_float(t.get("difficulty_multiplier", 1.0), 1.0))
            diff_map[name] = diff
            impacts_map[name] = impact
            raw_w_map[name] = (impact + prior_f) * diff
    # Case 2: List or tuple (could be list of topic dicts OR list of numeric raw weights)
    elif isinstance(raw_weights_or_manifest, (list, tuple)):
        if not raw_weights_or_manifest:
            return 0
        has_dicts = any(isinstance(x, dict) for x in raw_weights_or_manifest)
        if has_dicts:
            for i, item in enumerate(raw_weights_or_manifest):
                if isinstance(item, dict):
                    name = str(item.get("topic_name", "") or f"Topic_{i}")
                    impact = max(0.0, _safe_float(item.get("question_impact_score", item.get("existing_questions", 0.0)), 0.0))
                    diff = max(0.1, _safe_float(item.get("difficulty_multiplier", 1.0), 1.0))
                else:
                    name = f"Topic_{i}"
                    impact = 0.0
                    diff = 1.0
                diff_map[name] = diff
                impacts_map[name] = impact
                raw_w_map[name] = (impact + prior_f) * diff
        else:
            diff_in = difficulty_map or {}
            impact_in = topic_impacts or {}
            for i, v in enumerate(raw_weights_or_manifest):
                name = f"Topic_{i}"
                diff = max(0.1, _safe_float(diff_in.get(name, 1.5), 1.5))
                impact = max(0.0, _safe_float(impact_in.get(name, 0.0), 0.0))
                raw_w = max(0.0, _safe_float(v, 0.0))
                diff_map[name] = diff
                impacts_map[name] = impact
                raw_w_map[name] = raw_w
    # Case 3: Dict of topic -> raw_weight
    elif isinstance(raw_weights_or_manifest, dict):
        diff_in = difficulty_map or {}
        impact_in = topic_impacts or {}
        for k, v in raw_weights_or_manifest.items():
            name = str(k)
            diff = max(0.1, _safe_float(diff_in.get(name, 1.5), 1.5))
            impact = max(0.0, _safe_float(impact_in.get(name, 0.0), 0.0))
            raw_w = max(0.0, _safe_float(v, 0.0))
            diff_map[name] = diff
            impacts_map[name] = impact
            raw_w_map[name] = raw_w
    else:
        return 0

    N = len(raw_w_map)
    if N == 0:
        return 0

    min_q_raw = _safe_float(min_questions, None) if min_questions is not None else None
    max_q_raw = _safe_float(max_questions, 50.0) if max_questions is not None else 50.0

    min_q = max(0, int(round(min_q_raw))) if min_q_raw is not None else min(5, max(1, N))
    max_q = max(min_q, int(round(max_q_raw)))

    total_raw_w = sum(raw_w_map.values())
    if total_raw_w <= 0:
        return min_q

    # Calculate weighted difficulty density (CBSE reference standard is 1.5)
    weighted_diff = sum(raw_w_map[t] * diff_map.get(t, 1.5) for t in raw_w_map) / total_raw_w
    diff_density = max(0.1, weighted_diff / 1.5)

    # Count significant topics (active questions or non-negligible relative weight share)
    sig_count = 0
    for t, raw_w in raw_w_map.items():
        impact = impacts_map.get(t, 0.0)
        rel_share = raw_w / total_raw_w
        if impact > 0.0 or rel_share >= (0.5 / N):
            sig_count += 1

    if sig_count == 0:
        sig_count = max(1, N)

    # Core dynamic question budgeting formula
    q_raw = (c1_f * sig_count + c2_f * math.sqrt(total_raw_w)) * diff_density
    dynamic_target = max(min_q, min(max_q, int(round(q_raw))))
    return int(dynamic_target)


def apportion_quotas(
    topics: List[Dict[str, Any]],
    target_questions: Union[int, str],
    prior: Union[float, str] = PRIOR_WEIGHT,
) -> List[Dict[str, Any]]:
    """
    Apportions target_questions across a list of topic dictionaries using Hamilton's
    largest remainder method based on exact mathematical shares:
      exact_share_j = (raw_weight_j / total_raw_weight) * target_questions
    Guarantees remainder is strictly non-negative and sum(allocated_quota) == target_questions.
    Returns a new list of topic dicts with 'allocated_quota' set.
    """
    if not topics or not isinstance(topics, list):
        return []

    try:
        target_q = max(0, int(target_questions))
    except (ValueError, TypeError):
        target_q = 0

    if target_q <= 0:
        return [{**t, "allocated_quota": 0} if isinstance(t, dict) else t for t in topics]

    prior_f = _safe_float(prior, PRIOR_WEIGHT)
    raw_weights: List[float] = []
    for t in topics:
        if not isinstance(t, dict):
            raw_weights.append(0.0)
            continue
        impact = max(0.0, _safe_float(t.get("question_impact_score", t.get("existing_questions", 0.0)), 0.0))
        diff = max(0.1, _safe_float(t.get("difficulty_multiplier", 1.0), 1.0))
        raw_weights.append((impact + prior_f) * diff)

    total_w = sum(raw_weights)
    if total_w > 0:
        exact_shares = [(w / total_w) * target_q for w in raw_weights]
        floor_shares = [int(s) for s in exact_shares]
        remainder = target_q - sum(floor_shares)

        # Sort indices by fraction descending, then raw_weight descending
        indexed = sorted(
            range(len(topics)),
            key=lambda i: (exact_shares[i] - floor_shares[i], raw_weights[i]),
            reverse=True,
        )
        for r in range(max(0, remainder)):
            floor_shares[indexed[r % len(indexed)]] += 1

        result = []
        for i, t in enumerate(topics):
            new_t = dict(t) if isinstance(t, dict) else {"topic_name": str(t)}
            new_t["allocated_quota"] = floor_shares[i]
            result.append(new_t)
        return result
    else:
        base = target_q // len(topics)
        rem = target_q % len(topics)
        result = []
        for i, t in enumerate(topics):
            new_t = dict(t) if isinstance(t, dict) else {"topic_name": str(t)}
            new_t["allocated_quota"] = base + (1 if i < rem else 0)
            result.append(new_t)
        return result


def synthesize_topic_weights(
    toc_topics: List[str],
    question_results: List[Dict[str, str]],
    difficulty_results: Dict[str, float],
    prior: float = PRIOR_WEIGHT,
    target_questions: Optional[Union[int, str]] = None,
    dynamic_target: bool = False,
    min_questions: Optional[int] = None,
    max_questions: int = 50,
    clamp_manual: bool = False,
    validate_range: bool = False,
) -> Dict[str, Any]:
    """
    Synthesizes chapter-wide topic weights and optional allocation quotas:
    1. Aggregates question impact per topic: I_j = sum(w_type(q))
    2. Applies topic difficulty multiplier: W_j = (I_j + prior) * D_j
    3. Normalizes across chapter: P_j = W_j / sum(W_k)
    4. Computes dynamic question count if target_questions is 'auto' or dynamic_target is True.
    5. Computes Hamilton largest remainder quota allocations if target_questions is given or dynamically determined.
    """
    if not toc_topics:
        return {
            "chapter_total_questions_extracted": len(question_results),
            "topics": [],
        }

    # Case-insensitive map for matching question results and difficulty results back to canonical TOC topics
    canonical_topics = list(toc_topics)
    topic_clean_map = {t.strip().lower(): t for t in canonical_topics}

    topic_impacts: Dict[str, float] = defaultdict(float)
    topic_q_counts: Dict[str, int] = defaultdict(int)

    for res in question_results:
        raw_topic = str(res.get("topic") or "").strip()
        q_type = res.get("type", "conceptual")
        weight = get_type_weight(q_type)

        matched_topic = None
        if raw_topic in canonical_topics:
            matched_topic = raw_topic
        elif raw_topic.lower() in topic_clean_map:
            matched_topic = topic_clean_map[raw_topic.lower()]
        else:
            # 1. Number prefix match with boundary check (e.g. "1.1" matches "1.1 Types of Solutions")
            for ct in canonical_topics:
                if re.match(r"^" + re.escape(raw_topic) + r"(?:\s|:|-|$)", ct):
                    matched_topic = ct
                    break
            # 2. Match unnumbered titles
            if not matched_topic:
                raw_unnum = re.sub(r"^\s*(?:\d+\.)*\d+\s*", "", raw_topic).strip().lower()
                if raw_unnum:
                    for ct in canonical_topics:
                        ct_unnum = re.sub(r"^\s*(?:\d+\.)*\d+\s*", "", ct).strip().lower()
                        if raw_unnum == ct_unnum:
                            matched_topic = ct
                            break
            # 3. Substring match fallback for long names
            if not matched_topic:
                for ct in canonical_topics:
                    if len(raw_topic) >= 4 and (ct.lower() in raw_topic.lower() or raw_topic.lower() in ct.lower()):
                        matched_topic = ct
                        break

        if not matched_topic and canonical_topics:
            matched_topic = canonical_topics[0]

        if matched_topic:
            topic_impacts[matched_topic] += weight
            topic_q_counts[matched_topic] += 1

    # Canonicalize difficulty results
    clean_diff_map: Dict[str, float] = {}
    for k, v in difficulty_results.items():
        k_clean = str(k).strip()
        if k_clean in canonical_topics:
            clean_diff_map[k_clean] = _safe_float(v, 1.0)
        elif k_clean.lower() in topic_clean_map:
            clean_diff_map[topic_clean_map[k_clean.lower()]] = _safe_float(v, 1.0)

    # Raw weights calculation: W_j = (I_j + prior) * D_j
    prior_f = _safe_float(prior, PRIOR_WEIGHT)
    raw_weights: Dict[str, float] = {}
    for topic in canonical_topics:
        impact = topic_impacts[topic]
        diff = clean_diff_map.get(topic, 1.0)
        raw_weights[topic] = (impact + prior_f) * diff

    total_weight = sum(raw_weights.values())
    if total_weight > 0:
        normalized_weights = {
            topic: round(raw_weights[topic] / total_weight, 4)
            for topic in canonical_topics
        }
    else:
        equal_weight = round(1.0 / len(canonical_topics), 4)
        normalized_weights = {topic: equal_weight for topic in canonical_topics}

    # Determine if dynamic target question budgeting is requested
    is_dynamic = False
    if dynamic_target:
        is_dynamic = True
    elif isinstance(target_questions, str) and target_questions.strip().lower() in ("auto", "dynamic"):
        is_dynamic = True
    elif target_questions is not None:
        if clamp_manual or validate_range:
            manual_target, is_dyn = validate_target_questions(target_questions, clamp=clamp_manual)
            if is_dyn:
                is_dynamic = True
            else:
                is_dynamic = False
                target_questions = manual_target
        else:
            try:
                target_questions = max(0, int(round(float(target_questions))))
                is_dynamic = False
            except (ValueError, TypeError):
                is_dynamic = True

    if is_dynamic:
        target_questions = compute_dynamic_target_questions(
            raw_weights_or_manifest=raw_weights,
            topic_impacts=topic_impacts,
            difficulty_map=clean_diff_map,
            min_questions=min_questions,
            max_questions=max_questions,
            prior=prior_f,
        )

    # Format base topic entries
    topic_entries = []
    for topic in canonical_topics:
        entry: Dict[str, Any] = {
            "topic_name": topic,
            "existing_questions": topic_q_counts[topic],
            "question_impact_score": round(topic_impacts[topic], 2),
            "difficulty_multiplier": round(clean_diff_map.get(topic, 1.0), 2),
            "final_weight": normalized_weights[topic],
        }
        topic_entries.append(entry)

    # Allocation quotas using Largest Remainder (Hamilton's apportionment) method
    if target_questions is not None:
        topic_entries = apportion_quotas(topic_entries, target_questions, prior=prior_f)

    manifest: Dict[str, Any] = {
        "chapter_total_questions_extracted": len(question_results),
        "topics": topic_entries,
    }
    if target_questions is not None:
        manifest["target_questions"] = int(target_questions)
        manifest["is_dynamic_target"] = is_dynamic
        manifest["target_questions_mode"] = "dynamic" if is_dynamic else "manual"

    return manifest


# ----------------- DATA INGESTION HELPER -----------------
async def _fetch_with_fallback(
    client: httpx.AsyncClient,
    primary_url: str,
    endpoint_type: str,
) -> Any:
    """
    Fetches JSON or LaTeX from primary input (file or HTTP endpoint).
    Caches parsed results in memory in _INGESTION_CACHE.
    If primary fails with connection error:
    1. Tries local file path if primary_url points to a file.
    2. Tries port 3000 fallback if primary was port 8000.
    3. Tries workspace uploaded input files (solutions_textbook_1to1.tex, toc_result.json, notebook_question_bank.json).
    4. Falls back to bundled data/chapter_emi.json if available.
    """
    clean_target = str(primary_url or "").strip()
    if clean_target.startswith("file://"):
        clean_target = clean_target[7:].lstrip("/")

    cache_key = f"{endpoint_type}:{clean_target}"
    if cache_key in _INGESTION_CACHE:
        return _INGESTION_CACHE[cache_key]

    # 1. Direct file check
    if os.path.isfile(clean_target):
        try:
            if clean_target.lower().endswith(".tex"):
                with open(clean_target, "r", encoding="utf-8") as f:
                    content = f.read()
                res = chunk_latex_document(content) if endpoint_type == "chunks" else content
                _INGESTION_CACHE[cache_key] = res
                return res
            elif clean_target.lower().endswith(".json"):
                with open(clean_target, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _INGESTION_CACHE[cache_key] = data
                return data
        except Exception:
            pass

    # 2. Try HTTP/HTTPS GET if it looks like an HTTP URL
    if clean_target.startswith("http://") or clean_target.startswith("https://"):
        host_key = re.sub(r"^https?://", "", clean_target).split("/")[0]
        if host_key not in _OFFLINE_HOSTS:
            try:
                resp = await client.get(clean_target, timeout=httpx.Timeout(5.0, connect=FAST_CONNECT_TIMEOUT))
                resp.raise_for_status()
                data = resp.json()
                _INGESTION_CACHE[cache_key] = data
                return data
            except (httpx.ConnectError, httpx.ConnectTimeout):
                _OFFLINE_HOSTS.add(host_key)
            except (httpx.RequestError, httpx.HTTPStatusError):
                pass

        # Port 3000 fallback
        fallback_url = FALLBACK_PORT_3000_MAP.get(clean_target)
        if fallback_url and fallback_url != clean_target:
            fallback_host = re.sub(r"^https?://", "", fallback_url).split("/")[0]
            if fallback_host not in _OFFLINE_HOSTS:
                try:
                    resp = await client.get(fallback_url, timeout=httpx.Timeout(5.0, connect=FAST_CONNECT_TIMEOUT))
                    resp.raise_for_status()
                    data = resp.json()
                    _INGESTION_CACHE[cache_key] = data
                    return data
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    _OFFLINE_HOSTS.add(fallback_host)
                except (httpx.RequestError, httpx.HTTPStatusError):
                    pass

    # 3. Check for uploaded workspace files
    workspace_candidates = [
        os.getcwd(),
        os.path.dirname(__file__),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
    ]
    file_map = {
        "chunks": ["solutions_textbook_1to1.tex", "chapter_chunks.json", "chunks.json"],
        "toc": ["toc_result.json", "toc.json", "chapter_toc.json"],
        "questions": ["notebook_question_bank.json", "questions.json", "question_bank.json"],
    }
    for base_dir in workspace_candidates:
        for fname in file_map.get(endpoint_type, []):
            fpath = os.path.join(base_dir, fname)
            if os.path.isfile(fpath):
                try:
                    if fpath.lower().endswith(".tex"):
                        with open(fpath, "r", encoding="utf-8") as f:
                            content = f.read()
                        res = chunk_latex_document(content) if endpoint_type == "chunks" else content
                        _INGESTION_CACHE[cache_key] = res
                        return res
                    elif fpath.lower().endswith(".json"):
                        with open(fpath, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        _INGESTION_CACHE[cache_key] = data
                        return data
                except Exception:
                    pass

    # 4. Fallback to bundled dataset
    possible_paths = [
        os.path.join(os.path.dirname(__file__), "data", "chapter_emi.json"),
        os.path.join(os.getcwd(), "cbse-generator", "data", "chapter_emi.json"),
        os.path.join(os.getcwd(), "data", "chapter_emi.json"),
    ]
    for p in possible_paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _INGESTION_CACHE[cache_key] = data
                return data
            except Exception:
                pass

    raise httpx.ConnectError(f"Could not connect to {primary_url} and no fallback dataset was available.")


# ----------------- MAIN END-TO-END PIPELINE -----------------
async def compute_chapter_weights(
    chunks_endpoint: str = DEFAULT_CHUNKS_ENDPOINT,
    toc_endpoint: str = DEFAULT_TOC_ENDPOINT,
    questions_endpoint: str = DEFAULT_QUESTIONS_ENDPOINT,
    ollama_url: str = DEFAULT_OLLAMA_SYSTEMONE_URL,
    concurrency: int = 4,
    target_questions: Optional[Union[int, str]] = None,
    dynamic_target: Optional[bool] = None,
    min_questions: Optional[int] = None,
    max_questions: int = 50,
    mock_mode: bool = False,
    client: Optional[httpx.AsyncClient] = None,
    clamp_manual: bool = True,
) -> Dict[str, Any]:
    """
    Executes the End-to-End Orchestration Pipeline:
    1. Concurrently fetches chunks, TOC, and questions from endpoints or local files.
    2. Aligns LaTeX chunk texts to corresponding TOC topics.
    3. Executes Pass 1: Question Classifier using Nimble /v1/systemone or fast heuristic engine.
    4. Executes Pass 2: Topic Technical Rigor Scorer using Nimble /v1/systemone or STEM rigor engine.
    5. Aggregates and synthesizes normalized weights and quotas deterministically.
    """
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0)
        close_client = True

    try:
        # Step 1: Ingest data from three endpoints/files concurrently
        print(f"Fetching chunks, TOC, and questions from endpoints/files...")

        chunks_raw, toc_raw, questions_raw = await asyncio.gather(
            _fetch_with_fallback(client, chunks_endpoint, "chunks"),
            _fetch_with_fallback(client, toc_endpoint, "toc"),
            _fetch_with_fallback(client, questions_endpoint, "questions"),
        )

        chunks_data = normalize_chunks(chunks_raw)
        toc_topics = normalize_toc_topics(toc_raw)
        questions = normalize_questions(questions_raw)
        question_records = normalize_question_records(questions_raw)

        print(f"Loaded {len(toc_topics)} TOC topics, {len(chunks_data)} chunks, {len(questions)} questions.")

        # Align chunk text by topic
        topic_texts = align_chunks_to_topics(chunks_data, toc_topics)

        # Semaphore protects local Ollama instance from VRAM spike
        sem = asyncio.Semaphore(concurrency)

        # Circuit Breaker: Fast probe of Ollama availability
        effective_mock = mock_mode
        if not mock_mode:
            try:
                probe_url = ollama_url.split("/v1")[0] + "/api/version"
                probe_res = await client.get(probe_url, timeout=0.5)
                if probe_res.status_code not in (200, 404, 405):
                    effective_mock = True
            except Exception:
                effective_mock = True

        if not mock_mode and effective_mock:
            print(f"Notice: Ollama endpoint ({ollama_url}) offline. Fast heuristic decision engine activated.")

        # Step 2: Pass 1 (Question Classification)
        async def bounded_classify(idx: int):
            rec = question_records[idx] if idx < len(question_records) else {}
            q_latex = questions[idx] if idx < len(questions) else ""
            if effective_mock:
                # If pre-annotated record has topic, resolve directly
                if rec.get("topic"):
                    raw_top = str(rec["topic"]).strip()
                    matched = None
                    for ct in toc_topics:
                        if ct.lower() == raw_top.lower() or re.match(r"^" + re.escape(raw_top) + r"(?:\s|:|-|$)", ct):
                            matched = ct
                            break
                    if not matched:
                        raw_unnum = re.sub(r"^\s*(?:\d+\.)*\d+\s*", "", raw_top).strip().lower()
                        for ct in toc_topics:
                            ct_unnum = re.sub(r"^\s*(?:\d+\.)*\d+\s*", "", ct).strip().lower()
                            if raw_unnum and raw_unnum == ct_unnum:
                                matched = ct
                                break
                    if matched:
                        q_type = get_canonical_type(
                            rec.get("type"),
                            is_derivation=rec.get("isDerivation", False),
                            is_numerical=rec.get("isNumerical", False),
                        )
                        return {"topic": matched, "type": q_type}
                return mock_classify_question(q_latex, toc_topics)

            async with sem:
                return await classify_question_with_nimble(
                    client=client,
                    question_latex=q_latex,
                    toc_topics=toc_topics,
                    nimble_url=ollama_url,
                    mock_mode=effective_mock,
                )

        # Step 3: Pass 2 (Topic Rigor Scoring)
        async def bounded_score(t: str):
            if effective_mock:
                return t, mock_score_topic_difficulty(t, topic_texts.get(t, ""))
            async with sem:
                diff = await score_topic_difficulty_with_nimble(
                    client=client,
                    topic_name=t,
                    topic_latex_text=topic_texts.get(t, ""),
                    nimble_url=ollama_url,
                    mock_mode=effective_mock,
                )
                return t, diff

        # Execute Pass 1 and Pass 2 concurrently
        print(f"Executing Nimble Pass 1 ({len(questions)} questions) & Pass 2 ({len(toc_topics)} topics) with concurrency={concurrency}...")
        question_coros = [bounded_classify(i) for i in range(len(questions))]
        topic_coros = [bounded_score(t) for t in toc_topics]

        question_results, difficulty_pairs = await asyncio.gather(
            asyncio.gather(*question_coros),
            asyncio.gather(*topic_coros),
        )
        difficulty_results = dict(difficulty_pairs)

        # Step 4: Synthesize chapter-wide weights and allocation quotas
        # Dual mode resolution:
        # Default (when no number manually chosen or explicitly auto/dynamic): Fully dynamic mode.
        # Manual total selection: Fixed integer total in [10, 50] with dynamic topic allocation.
        if dynamic_target is True:
            is_dynamic_mode = True
            resolved_target = "auto"
        else:
            val_target, is_dyn = validate_target_questions(target_questions, clamp=clamp_manual)
            if is_dyn:
                is_dynamic_mode = True
                resolved_target = "auto"
            else:
                is_dynamic_mode = False
                resolved_target = val_target

        print("Synthesizing normalized topic weights and allocation quotas deterministically...")
        chapter_manifest = synthesize_topic_weights(
            toc_topics=toc_topics,
            question_results=question_results,
            difficulty_results=difficulty_results,
            prior=PRIOR_WEIGHT,
            target_questions=resolved_target,
            dynamic_target=is_dynamic_mode,
            min_questions=min_questions,
            max_questions=max_questions,
            clamp_manual=clamp_manual,
        )

        # Step 5: Extract chapter metadata if present in inputs
        detected_subject = None
        detected_grade = None
        detected_chapter = None
        for raw_obj in (toc_raw, questions_raw):
            if isinstance(raw_obj, dict) and "chapter" in raw_obj:
                chap_str = str(raw_obj["chapter"]).strip()
                detected_chapter = chap_str
                m_grade = re.search(r"Class\s*(\d+)", chap_str, re.IGNORECASE)
                if m_grade:
                    detected_grade = m_grade.group(1)
                for subj in ("Physics", "Chemistry", "Mathematics", "Biology"):
                    if subj.lower() in chap_str.lower():
                        detected_subject = subj
                        break
                break

        if detected_chapter:
            chapter_manifest["chapter_title"] = detected_chapter
        if detected_subject:
            chapter_manifest["subject"] = detected_subject
        if detected_grade:
            chapter_manifest["grade"] = detected_grade

        return chapter_manifest

    finally:
        if close_client:
            await client.aclose()


# ----------------- CBSE QUESTION GENERATION INTEGRATION -----------------

DEFAULT_CBSE_GENERATE_URL = os.getenv("CBSE_GENERATE_URL", "http://localhost:3000/api/generate")
DEFAULT_CBSE_PROMPT_TEMPLATE_URL = os.getenv("CBSE_PROMPT_TEMPLATE_URL", "http://localhost:3000/api/prompt-template")

CBSE_SYSTEM_PROMPT = """# SYSTEM CONTEXT: CBSE QUESTION GENERATOR (TOPIC-WISE LATEX PIPELINE)

You are an expert CBSE Senior Secondary (Classes 10, 11, and 12) Paper Setter and NCERT Content Specialist.
Your sole task is to generate authentic, high-quality CBSE board examination questions with step-by-step marking schemes from a provided topic-wise LaTeX chunk.

---

## 1. INPUT CONTRACT

You will receive the following parameters:
- {subject}: (e.g., Physics, Chemistry, Mathematics, Biology)
- {grade}: (e.g., 10, 11, or 12)
- {chapter_title}: (e.g., "Electrochemistry", "Ray Optics", "Electromagnetic Induction")
- {topic_name}: The specific topic or section title covered by this chunk
- {topic_weight}: (Optional) Relative weight/importance score of this topic within the chapter (e.g., 0.5945 or 59.5%)
- {allocated_quota}: (Optional) Target number of questions to generate for this topic based on chapter weighting
- {latex_chunk}: The raw LaTeX source text for this specific topic
- {requested_selection}: The user's requested specification for questions (e.g., "3 MCQs and 1 Assertion-Reason", "Only 3-mark Numericals", "1 Case Study", or "All applicable 2M and 5M questions")

---

## 2. STRICT SELECTION & FILTERING RULE (MANDATORY)

1. **Only Generate What Is Requested:**
   - Look strictly at `{requested_selection}` and `{allocated_quota}`.
   - **DO NOT** generate any question type, mark value, or quantity that was not requested.
   - If the user asks for *only 1-mark MCQs*, output **only** 1-mark MCQs.
   - If the user asks for *2 questions of 3 marks*, output **exactly 2** questions of 3 marks.
   - When `{topic_weight}` and `{allocated_quota}` are provided, calibrate question cognitive depth, difficulty tier, and coverage to match the assigned weight.
2. **Chunk Feasibility Check:**
   - If a requested format is mathematically or conceptually impossible from the provided topic chunk (e.g., the user requested a "5-mark Derivation", but the chunk contains only purely descriptive qualitative definitions with zero formulas), state:
     `[SKIPPED: {Requested Type} - Topic chunk lacks required theoretical/mathematical basis]`
   - Do NOT invent out-of-syllabus formulas or concepts outside `{latex_chunk}`.

---

## 3. LATEX CHUNK PROCESSING RULES

1. **Math Formatting:** 
   - Render all mathematical equations, symbols, units, and formulas in standard LaTeX math format: inline `$ ... $` or display `$$ ... $$`.
2. **Handling Graphics / Figures (`\\includegraphics`, Figures):**
   - LaTeX textbook chunks often reference diagrams or apparatus setups via `\\includegraphics{...}` or `Figure X`.
   - If a question relies on an apparatus, circuit, or diagram, provide a clear, standalone **"Diagram Setup / Visual Description"** in text within the question so it is 100% self-contained without needing the image file.
3. **LaTeX Markup Cleansing:**
   - Ignore formatting tags (`\\section`, `\\textbf`, `\\label`, `\\ref`, `\\cite`, `\\vspace`, etc.). Extract only the core physical/chemical laws, definitions, proofs, data tables, and conceptual assertions.

---

## 4. CBSE QUESTION TAXONOMY & BLUEPRINT

Every generated question must belong to one of these official CBSE categories:

| Category | Marks | CBSE Design Specifications |
| :--- | :---: | :--- |
| **Objective — MCQ** | **1M** | 4 plausible options (A, B, C, D) with exactly one correct option. Include authentic distractors based on common student misconceptions. |
| **Assertion-Reason (A-R)** | **1M** | Provide Statement (A) and Reason (R) with standard 4 CBSE options:<br>(a) Both A and R are true and R is the correct explanation of A.<br>(b) Both A and R are true but R is NOT the correct explanation of A.<br>(c) A is true but R is false.<br>(d) A is false but R is true. |
| **Very Short Answer (VSA)** | **2M** | 2–3 sentences. Core law statement + condition, definition + SI unit, or direct conceptual reasoning. |
| **Short Answer (SA)** | **3M** | Structured 4–6 sentence answer. Explaining a principle, drawing a conceptual graph, or a standard 2-step numerical. |
| **Case-Based / Source-Based** | **4M** | A 60–100 word context-rich passage based on the chunk, followed by 3 sub-questions:<br>• Sub-part (a): 1 Mark (recall/direct)<br>• Sub-part (b): 1 Mark (conceptual/reasoning)<br>• Sub-part (c): 2 Marks (application/calculation with an internal "OR" alternative) |
| **Long Answer (LA) / Derivation** | **5M** | Comprehensive multi-step proof/derivation from first principles (numbered steps), a 3+ step numerical, or a structured multi-part conceptual question (e.g., (a) 3 marks + (b) 2 marks). |

---

## 5. QUALITY & EXAM STANDARDS

1. **Originality (No NCERT Duplication):** Do not copy textbook solved examples or exercises verbatim. Swap numbers, change context, or invert what is "Given" vs. what is "To Find".
2. **CBSE Competency Alignment:** Focus on real-world application, diagram/graph interpretation, and analytical reasoning (minimum 50% competency-based per CBSE guidelines).
3. **Step-Wise Marking Scheme:** CBSE answers require explicit step-marking (e.g., formula = $1/2$ mark, substitution = $1$ mark, final value with units = $1/2$ mark).
4. **Tier Classification:**
   - 🔴 **Tier 1 (Core / Must-Know):** Standard board staples, frequent derivations, mandatory laws, recurring board traps.
   - 🟡 **Tier 2 (Higher Order / Differentiator):** Multi-concept linkages, edge cases, tricky graphical analysis.

---

## 6. OUTPUT FORMAT (Per Question)

Generate each question using this exact structure:

---
### Q[N] | [Category Name] | [Marks] Mark(s) | [🔴 Tier 1 / 🟡 Tier 2]
- **Target Concept:** [Sub-concept/law from the chunk]
- **Question:**
  [Full question text formatted in clean Markdown with LaTeX math `$ ... $`]
  [If MCQ / A-R: list all options clearly]
  [If Case-Based: provide passage first, followed by parts (a), (b), (c) and internal choice]
  [If diagram required: provide "Diagram Setup: ..."]

- **Marking Scheme & Solution:**
  - [Step 1 / Key Point 1] — **[Marks allocated, e.g., 1/2 M]**
  - [Step 2 / Key Point 2] — **[Marks allocated, e.g., 1 M]**
  - [Final Answer with SI unit / Conclusion] — **[Marks allocated, e.g., 1/2 M]**

- **Why this Tier:** [1 concise sentence on board exam relevance]
---

## 7. EXECUTION
Read `{latex_chunk}`, inspect `{requested_selection}`, and produce ONLY the requested questions following the exact template above."""

CBSE_USER_PROMPT_TEMPLATE = """INPUT PARAMETERS:
- {subject}: {subject}
- {grade}: {grade}
- {chapter_title}: {chapter_title}
- {topic_name}: {topic_name}
- {topic_weight}: {topic_weight}
- {allocated_quota}: {allocated_quota}
- {requested_selection}: {requested_selection}

LATEX CHUNK:
```latex
{latex_chunk}
```

Strictly obey all guidelines in the system context. Generate ONLY the requested questions following the exact output structure."""


def render_cbse_user_prompt(
    subject: str = "Physics",
    grade: str = "12",
    chapter_title: str = "Electromagnetic Induction",
    topic_name: str = "General Topic",
    topic_weight: Optional[float] = None,
    allocated_quota: Optional[int] = None,
    latex_chunk: str = "",
    requested_selection: str = "1 Short Answer (3M)",
    template: Optional[str] = None,
) -> str:
    """Formats the CBSE generator user prompt with topic parameters, weight, quota, and LaTeX chunk."""
    if template:
        text = template
        # Protect literal labels like "- {subject}: " with placeholders so {key} substitution doesn't corrupt them
        text = text.replace("- {subject}: {subject}", f"- __SUBJ_LBL__: {subject}")
        text = text.replace("- {grade}: {grade}", f"- __GRADE_LBL__: {grade}")
        text = text.replace("- {chapter_title}: {chapter_title}", f"- __CHAP_LBL__: {chapter_title}")
        text = text.replace("- {topic_name}: {topic_name}", f"- __TOPIC_LBL__: {topic_name}")
        text = text.replace("- {requested_selection}: {requested_selection}", f"- __REQ_LBL__: {requested_selection}")

        weight_lines = ""
        if topic_weight is not None:
            weight_lines += f"- {{topic_weight}}: {topic_weight}\n"
        if allocated_quota is not None:
            weight_lines += f"- {{allocated_quota}}: {allocated_quota}\n"
        text = text.replace("{weight_lines}", weight_lines)

        if topic_weight is not None:
            text = text.replace("- {topic_weight}: {topic_weight}", f"- __WEIGHT_LBL__: {topic_weight}")
            text = text.replace("{topic_weight}", str(topic_weight))
        else:
            text = re.sub(r"^.*\{topic_weight\}.*$\n?", "", text, flags=re.MULTILINE)
            text = re.sub(r"^.*__WEIGHT_LBL__.*$\n?", "", text, flags=re.MULTILINE)

        if allocated_quota is not None:
            text = text.replace("- {allocated_quota}: {allocated_quota}", f"- __QUOTA_LBL__: {allocated_quota}")
            text = text.replace("{allocated_quota}", str(allocated_quota))
        else:
            text = re.sub(r"^.*\{allocated_quota\}.*$\n?", "", text, flags=re.MULTILINE)
            text = re.sub(r"^.*__QUOTA_LBL__.*$\n?", "", text, flags=re.MULTILINE)

        text = text.replace("{subject}", subject)
        text = text.replace("{grade}", grade)
        text = text.replace("{chapter_title}", chapter_title)
        text = text.replace("{topic_name}", topic_name)
        text = text.replace("{requested_selection}", requested_selection)
        text = text.replace("{latex_chunk}", latex_chunk)

        # Restore protected literal labels
        text = text.replace("__SUBJ_LBL__", "{subject}")
        text = text.replace("__GRADE_LBL__", "{grade}")
        text = text.replace("__CHAP_LBL__", "{chapter_title}")
        text = text.replace("__TOPIC_LBL__", "{topic_name}")
        text = text.replace("__WEIGHT_LBL__", "{topic_weight}")
        text = text.replace("__QUOTA_LBL__", "{allocated_quota}")
        text = text.replace("__REQ_LBL__", "{requested_selection}")

        return text.strip()

    weight_lines = ""
    if topic_weight is not None:
        weight_lines += f"- {{topic_weight}}: {topic_weight}\n"
    if allocated_quota is not None:
        weight_lines += f"- {{allocated_quota}}: {allocated_quota}\n"

    return f"""INPUT PARAMETERS:
- {{subject}}: {subject}
- {{grade}}: {grade}
- {{chapter_title}}: {chapter_title}
- {{topic_name}}: {topic_name}
{weight_lines}- {{requested_selection}}: {requested_selection}

LATEX CHUNK:
```latex
{latex_chunk}
```

Strictly obey all guidelines in the system context. Generate ONLY the requested questions following the exact output structure."""


def build_requested_selection(
    allocated_quota: int,
    difficulty_multiplier: float = 1.0,
    final_weight: float = 0.0,
    preferred_types: Optional[List[str]] = None,
) -> str:
    """
    Constructs the CBSE requested_selection string from a topic's allocated quota and difficulty/weight.
    Translates quotas into an authentic mix of MCQs, Assertion-Reasons, VSAs, SAs, Case Studies, and Derivations.
    """
    if allocated_quota <= 0:
        return "0 questions"

    if preferred_types:
        parts = []
        base = allocated_quota // len(preferred_types)
        rem = allocated_quota % len(preferred_types)
        for i, pt in enumerate(preferred_types):
            cnt = base + (1 if i < rem else 0)
            if cnt > 0:
                parts.append(f"{cnt} {pt}")
        return ", ".join(parts)

    is_high_rigor = difficulty_multiplier >= 1.5 or final_weight >= 0.40

    if allocated_quota == 1:
        if is_high_rigor:
            return "1 Short Answer (3M)"
        return "1 Objective — MCQ (1M)"

    elif allocated_quota == 2:
        if is_high_rigor:
            return "1 Short Answer (3M), 1 Long Answer / Derivation (5M)"
        return "1 Objective — MCQ (1M), 1 Very Short Answer (2M)"

    elif allocated_quota == 3:
        if is_high_rigor:
            return "1 Objective — MCQ (1M), 1 Short Answer (3M), 1 Long Answer / Derivation (5M)"
        return "1 Objective — MCQ (1M), 1 Assertion-Reason (1M), 1 Very Short Answer (2M)"

    elif allocated_quota == 4:
        if is_high_rigor:
            return "1 Objective — MCQ (1M), 1 Short Answer (3M), 1 Case-Based (4M), 1 Long Answer / Derivation (5M)"
        return "1 Objective — MCQ (1M), 1 Assertion-Reason (1M), 1 Very Short Answer (2M), 1 Short Answer (3M)"

    elif allocated_quota == 5:
        if is_high_rigor:
            return "1 Objective — MCQ (1M), 1 Assertion-Reason (1M), 1 Short Answer (3M), 1 Case-Based (4M), 1 Long Answer / Derivation (5M)"
        return "2 Objective — MCQs (1M), 1 Assertion-Reason (1M), 1 Very Short Answer (2M), 1 Short Answer (3M)"

    elif allocated_quota == 6:
        if is_high_rigor:
            return "2 Objective — MCQs (1M), 1 Assertion-Reason (1M), 1 Short Answer (3M), 1 Case-Based (4M), 1 Long Answer / Derivation (5M)"
        return "2 Objective — MCQs (1M), 1 Assertion-Reason (1M), 2 Very Short Answers (2M), 1 Short Answer (3M)"

    else:
        # Large quota (>= 7): allocate across categories proportionally
        if is_high_rigor:
            n_la = max(1, allocated_quota // 5)
            n_case = max(1, allocated_quota // 6)
            n_sa = max(1, allocated_quota // 4)
            n_vsa = max(1, allocated_quota // 6)
            n_ar = 1
            n_mcq = max(1, allocated_quota - (n_la + n_case + n_sa + n_vsa + n_ar))
        else:
            n_mcq = max(2, allocated_quota // 3)
            n_ar = max(1, allocated_quota // 6)
            n_vsa = max(2, allocated_quota // 4)
            n_sa = max(1, allocated_quota // 5)
            n_case = 1 if allocated_quota >= 8 else 0
            n_la = 1 if allocated_quota >= 10 else 0

        total = n_mcq + n_ar + n_vsa + n_sa + n_case + n_la
        diff = allocated_quota - total
        n_mcq = max(1, n_mcq + diff)

        parts = []
        if n_mcq > 0:
            parts.append(f"{n_mcq} Objective — MCQ{'s' if n_mcq > 1 else ''} (1M)")
        if n_ar > 0:
            parts.append(f"{n_ar} Assertion-Reason (1M)")
        if n_vsa > 0:
            parts.append(f"{n_vsa} Very Short Answer{'s' if n_vsa > 1 else ''} (2M)")
        if n_sa > 0:
            parts.append(f"{n_sa} Short Answer{'s' if n_sa > 1 else ''} (3M)")
        if n_case > 0:
            parts.append(f"{n_case} Case-Based (4M)")
        if n_la > 0:
            parts.append(f"{n_la} Long Answer / Derivation{'s' if n_la > 1 else ''} (5M)")
        return ", ".join(parts)


def parse_selection_counts(
    requested_selection: str,
    allocated_quota: Optional[int] = None,
) -> List[Tuple[str, int]]:
    """
    Parses requested_selection string into an ordered list of (category, count).
    Valid categories: 'mcq', 'ar', 'vsa', 'sa', 'case', 'la'.
    If allocated_quota is specified, guarantees the total count matches allocated_quota.
    """
    sel = requested_selection or ""
    parts = [p.strip() for p in sel.split(",") if p.strip()]

    category_counts: List[Tuple[str, int]] = []

    for part in parts:
        m = re.match(r"^\s*(\d+)\s*(.*)", part)
        if m:
            count = int(m.group(1))
            desc = m.group(2).lower()
        else:
            count = 1
            desc = part.lower()

        cat = None
        if "assertion" in desc or "a-r" in desc:
            cat = "ar"
        elif "very short" in desc or "vsa" in desc or "2m" in desc or "2-mark" in desc:
            cat = "vsa"
        elif re.search(r"(?<!very\s)short answer|\b(?<!v)sa\b|\b3m\b|\b3-mark\b", desc, re.IGNORECASE):
            cat = "sa"
        elif "case" in desc or "4m" in desc or "4-mark" in desc:
            cat = "case"
        elif "long" in desc or "derivation" in desc or "5m" in desc or "5-mark" in desc:
            cat = "la"
        elif "mcq" in desc or "objective" in desc or "1m" in desc or "1-mark" in desc:
            cat = "mcq"

        if cat:
            category_counts.append((cat, count))

    if not category_counts:
        category_counts = [("mcq", allocated_quota or 1)]

    # If allocated_quota is specified, calibrate total questions to match allocated_quota
    if allocated_quota is not None and allocated_quota > 0:
        current_total = sum(c for _, c in category_counts)
        if current_total != allocated_quota:
            diff = allocated_quota - current_total
            if diff > 0:
                # Add remainder to primary category (first category or mcq)
                first_cat, first_count = category_counts[0]
                category_counts[0] = (first_cat, first_count + diff)
            else:
                new_counts = []
                remaining = allocated_quota
                for cat, cnt in category_counts:
                    take = min(cnt, remaining)
                    if take > 0:
                        new_counts.append((cat, take))
                        remaining -= take
                category_counts = new_counts

    return category_counts


def _build_mock_question(
    topic_name: str,
    latex_chunk: str,
    subject: str,
    grade: str,
    chapter_title: str,
    category: str,
    index: int,
    q_num: int,
) -> str:
    """Generates a single authentic CBSE question for the specified category and index."""
    t_low = (topic_name or "").lower()
    c_low = (latex_chunk or "").lower()
    s_low = (subject or "").lower()

    is_faraday = "faraday" in t_low or "faraday" in c_low or r"\mathcal{e}" in c_low
    is_lenz = "lenz" in t_low or "lenz" in c_low
    is_eddy = "eddy" in t_low or "damping" in c_low or "foucault" in c_low
    is_prism = "prism" in t_low or "ray optics" in s_low
    is_chem = (
        "chem" in s_low
        or "solution" in s_low
        or "solution" in c_low
        or any(k in t_low for k in ("solution", "solubility", "vapour", "vapor", "ideal", "colligative", "boiling", "freezing", "osmosis", "osmotic", "molar", "henry", "raoult", "concentration"))
    ) and not (is_faraday or is_lenz or is_eddy or is_prism)

    if category == "mcq":
        if is_faraday:
            if index == 0:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Faraday's Law and magnitude of induced electromotive force
- **Question:**
  A circular loop of wire of radius $r = 0.1\text{ m}$ is placed perpendicular to a uniform magnetic field. If the magnetic field decreases uniformly from $1.5\text{ T}$ to $0.5\text{ T}$ in $0.2\text{ s}$, the magnitude of the EMF induced in the loop is:
  (A) $0.157\text{ V}$
  (B) $0.314\text{ V}$
  (C) $0.050\text{ V}$
  (D) $0.628\text{ V}$

- **Marking Scheme & Solution:**
  - Area $A = \pi r^2 = \pi (0.1)^2 = 0.01\pi\text{ m}^2$. Rate of change $\frac{\Delta B}{\Delta t} = \frac{1.5 - 0.5}{0.2} = 5.0\text{ T/s}$. — **[1/2 M]**
  - Induced EMF $\mathcal{E} = A\frac{\Delta B}{\Delta t} = 0.01\pi \times 5.0 = 0.05\pi \approx 0.157\text{ V}$. Correct option is (A). — **[1/2 M]**

- **Why this Tier:** Core quantitative staple testing standard application of Faraday's Law of induction $\mathcal{E} = -d\Phi/dt$.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                turns = 50 + index * 10
                flux1 = turns * 0.4 * 0.02
                emf = flux1 / 0.1
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Magnetic flux linkage and induced EMF in rotating coil
- **Question:**
  A coil of __TURNS__ turns and area $0.02\text{ m}^2$ is rotated through $90^\circ$ from perpendicular to parallel in a uniform magnetic field $B = 0.4\text{ T}$ in $0.1\text{ s}$. The average induced EMF is:
  (A) __OPT_A__\text{ V}
  (B) __OPT_B__\text{ V}
  (C) __OPT_C__\text{ V}
  (D) $0.4\text{ V}$

- **Marking Scheme & Solution:**
  - Initial flux $\Phi_1 = N B A = __TURNS__ \times 0.4 \times 0.02\text{ Wb}$, final flux $\Phi_2 = 0$. — **[1/2 M]**
  - Average induced EMF $|\mathcal{E}| = \frac{\Delta\Phi}{\Delta t} = __EMF__\text{ V}$. Correct option is (B). — **[1/2 M]**

- **Why this Tier:** Core quantitative test of magnetic orientation change and Faraday's flux linkage.
---"""
                return (
                    q.replace("__QNUM__", str(q_num))
                    .replace("__TURNS__", str(turns))
                    .replace("__OPT_A__", f"${flux1 / 0.2:.1f}$")
                    .replace("__OPT_B__", f"${emf:.1f}$")
                    .replace("__OPT_C__", f"${flux1 / 0.05:.1f}$")
                    .replace("__EMF__", f"{emf:.1f}")
                )
        elif is_lenz:
            if index == 0:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Lenz's Law and direction of induced current
- **Question:**
  A bar magnet is dropped vertically along the axis of a horizontal fixed copper ring with its North pole pointing downward. As viewed from above, the direction of the induced current in the ring as the magnet approaches is:
  (A) Clockwise
  (B) Anticlockwise
  (C) First clockwise then anticlockwise
  (D) Zero throughout

- **Marking Scheme & Solution:**
  - As the North pole approaches, magnetic flux increases downward. To oppose this increase, the ring must develop a North pole on top. — **[1/2 M]**
  - A North pole corresponds to an anticlockwise current when viewed from above. Correct option is (B). — **[1/2 M]**

- **Why this Tier:** Classical CBSE board conceptual staple testing Lenz's direction rule and polarity conventions.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Lenz's law polarity during magnet withdrawal
- **Question:**
  When the North pole of a bar magnet is withdrawn away from the face of a stationary circular coil, the polarity induced on the facing coil surface is:
  (A) North pole to repel the magnet
  (B) South pole to attract the receding magnet
  (C) Uncharged dielectric boundary
  (D) Zero induced magnetic response

- **Marking Scheme & Solution:**
  - Receding North pole causes decreasing outward flux; Lenz's law opposes departure by inducing an attractive South pole. Correct option is (B). — **[1 M]**

- **Why this Tier:** Fundamental directional rule for receding magnetic dipoles.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_eddy:
            if index == 0:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Eddy currents and electromagnetic damping
- **Question:**
  The metallic core of a transformer is constructed from thin laminated iron sheets rather than a solid metal block primarily to:
  (A) Increase magnetic permeability
  (B) Minimize hysteresis losses
  (C) Reduce energy dissipation due to eddy currents
  (D) Prevent mechanical vibrations

- **Marking Scheme & Solution:**
  - Lamination with insulating lacquer breaks large circulating current loops into smaller paths with higher total resistance. — **[1/2 M]**
  - This drastically reduces $I^2 R$ heat loss caused by eddy currents. Correct option is (C). — **[1/2 M]**

- **Why this Tier:** Fundamental technical application question recurring in CBSE Class 12 Physics Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Applications of eddy currents in measuring instruments
- **Question:**
  The coil of a moving coil galvanometer is wound on a light metallic frame (former) to:
  (A) Make the magnetic field strictly radial
  (B) Provide electromagnetic damping to make it dead-beat
  (C) Increase torsional spring sensitivity
  (D) Protect the coil from electrostatic interference

- **Marking Scheme & Solution:**
  - Oscillations induce eddy currents in the metallic former, exerting opposing Lorentz force that brings the pointer rapidly to rest. Correct option is (B). — **[1 M]**

- **Why this Tier:** Classical technical application staple in CBSE Class 12 Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_prism:
            q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Condition for minimum deviation in a triangular prism
- **Question:**
  A light ray passes through an equilateral triangular glass prism ($A = 60^\circ$) at minimum deviation. If the angle of incidence is $48^\circ$, the angle of emergence $e$ and the angle of refraction $r_1$ at the first face are respectively:
  (A) $42^\circ$ and $30^\circ$
  (B) $48^\circ$ and $30^\circ$
  (C) $48^\circ$ and $24^\circ$
  (D) $30^\circ$ and $48^\circ$

- **Marking Scheme & Solution:**
  - At minimum deviation condition, $i = e$, hence $e = 48^\circ$. Also $r_1 = A/2 = 30^\circ$. Correct option is (B). — **[1 M]**

- **Why this Tier:** Fundamental board staple testing the symmetry condition ($i=e, r_1=r_2=A/2$) at minimum deviation.
---"""
            return q.replace("__QNUM__", str(q_num))
        elif is_chem:
            # Match 3-level subtopics first
            if re.search(r"\b1\.3\.1\b", t_low) or "solubility of a solid" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Effect of temperature on solubility of solids in liquids
- **Question:**
  If the dissolution of a solid solute in a liquid solvent is endothermic ($\Delta_{sol} H > 0$), according to Le Chatelier's principle, its solubility:
  (A) Increases with rise in temperature
  (B) Decreases with rise in temperature
  (C) Remains completely unaffected by temperature changes
  (D) Decreases initially then becomes infinite

- **Marking Scheme & Solution:**
  - For endothermic dissolution ($\text{Solute} + \text{Solvent} + \text{Heat} \rightleftharpoons \text{Solution}$), increasing temperature shifts equilibrium forward, increasing solubility. Correct option is (A). — **[1 M]**

- **Why this Tier:** Fundamental thermodynamic equilibrium application in CBSE Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.3\.2\b", t_low) or "solubility of a gas" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Henry's law constant ($K_H$) and solubility of gases in liquids
- **Question:**
  The value of Henry's constant $K_H$ for a gas:
  (A) Increases with rise in temperature, hence solubility decreases
  (B) Decreases with rise in temperature, hence solubility increases
  (C) Remains constant irrespective of temperature changes
  (D) Is identical for all gases in a given liquid solvent

- **Marking Scheme & Solution:**
  - Henry's law states $p = K_H x$. As temperature increases, $K_H$ increases, so mole fraction $x$ (solubility) decreases. Correct option is (A). — **[1 M]**

- **Why this Tier:** NCERT conceptual core tested recurringly in CBSE Class 12 Chemistry Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.4\.1\b", t_low) or "liquid-liquid solutions" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Raoult's law for volatile liquid-liquid mixtures
- **Question:**
  For a binary solution of volatile liquids 1 and 2, according to Raoult's law, the partial vapour pressure of component 1 is:
  (A) $p_1 = p_1^0 x_1$
  (B) $p_1 = p_1^0 / x_1$
  (C) $p_1 = p_1^0 + x_1$
  (D) $p_1 = p_1^0 (1 + x_1)$

- **Marking Scheme & Solution:**
  - Raoult's law states that for volatile liquids, the partial vapour pressure of each component equals vapour pressure of the pure component multiplied by its mole fraction. Correct option is (A). — **[1 M]**

- **Why this Tier:** Foundational governing equation for binary liquid mixtures.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.4\.2\b", t_low) or "special case" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Raoult's law as a special case of Henry's law
- **Question:**
  Raoult's law ($p_i = p_i^0 x_i$) becomes a special case of Henry's law ($p_i = K_H x_i$) when:
  (A) Henry's constant $K_H$ equals the vapour pressure of the pure volatile component $p_i^0$
  (B) The solution is highly concentrated
  (C) Both liquids are completely immiscible
  (D) The temperature is lowered to absolute zero

- **Marking Scheme & Solution:**
  - Comparing $p = K_H x$ and $p = p^0 x$, both state vapour pressure is proportional to mole fraction; they become identical when $K_H = p^0$. Correct option is (A). — **[1 M]**

- **Why this Tier:** Classical theoretical linkage in NCERT Section 1.4.2.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.4\.3\b", t_low) or "solids in liquids" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Mechanism of vapour pressure lowering by non-volatile solutes
- **Question:**
  When a non-volatile solid solute is dissolved in a pure liquid solvent, the vapour pressure of the resulting solution decreases because:
  (A) Non-volatile solute particles occupy part of the liquid surface, decreasing the fraction of surface covered by solvent molecules
  (B) Solute molecules boil at lower temperature than solvent
  (C) Solute molecules decompose the solvent into gases
  (D) Atmospheric pressure increases above the solution

- **Marking Scheme & Solution:**
  - Since vapour pressure depends on escaping solvent molecules from surface, reducing the solvent surface fraction reduces rate of evaporation. Correct option is (A). — **[1 M]**

- **Why this Tier:** Core physical mechanism explaining vapour pressure lowering in Section 1.4.3.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.5\.1\b", t_low):
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Identifying nearly ideal liquid binary mixtures
- **Question:**
  Which of the following binary pairs forms a nearly ideal solution obeying Raoult's law over all compositions?
  (A) n-Hexane and n-Heptane
  (B) Ethanol and Acetone
  (C) Chloroform and Acetone
  (D) Phenol and Aniline

- **Marking Scheme & Solution:**
  - n-Hexane and n-Heptane have nearly identical molecular structures and intermolecular forces ($A-B \approx A-A \approx B-B$), forming an ideal solution. Correct option is (A). — **[1 M]**

- **Why this Tier:** Textbook NCERT exemplar for ideal binary mixtures in Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.5\.2\b", t_low):
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Non-ideal solutions with positive deviation from Raoult's law
- **Question:**
  A mixture of ethanol and acetone exhibits a positive deviation from Raoult's law because:
  (A) Acetone molecules get between ethanol molecules, weakening intermolecular hydrogen bonds ($A-B < A-A, B-B$)
  (B) New strong hydrogen bonds form between acetone and ethanol
  (C) Volume of mixing is negative ($\Delta_{mix} V < 0$)
  (D) Enthalpy of mixing is exothermic ($\Delta_{mix} H < 0$)

- **Marking Scheme & Solution:**
  - Weakening of attractive forces increases vapour pressure above Raoult's law expectations, producing positive deviation. Correct option is (A). — **[1 M]**

- **Why this Tier:** Canonical CBSE benchmark on deviation mechanisms from Raoult's law.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.1\b", t_low) or "relative lowering" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Relative lowering of vapour pressure formula
- **Question:**
  According to Raoult's law, the relative lowering of vapour pressure ($\frac{p_1^0 - p_1}{p_1^0}$) for a dilute solution of a non-volatile solute equals:
  (A) The mole fraction of the solute ($x_2$)
  (B) The mole fraction of the solvent ($x_1$)
  (C) The molality of the solution
  (D) The total volume of the solution

- **Marking Scheme & Solution:**
  - Relative lowering $\frac{\Delta p_1}{p_1^0} = \frac{p_1^0 - p_1^0 x_1}{p_1^0} = 1 - x_1 = x_2$. Correct option is (A). — **[1 M]**

- **Why this Tier:** Direct examinable formula for the first colligative property.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.2\b", t_low) or "elevation of boiling" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Molal elevation constant ($K_b$) units and definition
- **Question:**
  The SI unit of the molal elevation constant $K_b$ (ebullioscopic constant) is:
  (A) $\text{K kg mol}^{-1}$
  (B) $\text{K mol kg}^{-1}$
  (C) $\text{kg mol}^{-1}\text{ K}^{-1}$
  (D) $\text{K m}^{-1}$

- **Marking Scheme & Solution:**
  - $K_b = \frac{\Delta T_b}{m} = \frac{\text{K}}{\text{mol kg}^{-1}} = \text{K kg mol}^{-1}$. Correct option is (A). — **[1 M]**

- **Why this Tier:** Fundamental unit identification question in CBSE Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.3\b", t_low) or "depression of freezing" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Depression of freezing point and antifreeze formulation
- **Question:**
  Ethylene glycol is mixed with water in car radiators in sub-zero climates primarily to:
  (A) Depress the freezing point of water to prevent radiator cracking
  (B) Increase the freezing point of the coolant
  (C) Catalyze radiator oxidation
  (D) Eliminate convective thermal transfer

- **Marking Scheme & Solution:**
  - Ethylene glycol acts as a non-volatile solute that depresses the freezing point ($\Delta T_f = K_f m$). Correct option is (A). — **[1 M]**

- **Why this Tier:** Frequent board question connecting colligative depression to practical antifreeze usage.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.4\b", t_low) or "osmosis and osmotic" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Isotonic solutions and osmotic equilibrium
- **Question:**
  An intravenous fluid injection must be strictly isotonic with human blood plasma. Which aqueous solution is isotonic with human blood cells?
  (A) $0.9\%$ (mass/volume) $\text{NaCl}$
  (B) $5.0\%$ (mass/volume) $\text{NaCl}$
  (C) $0.1\%$ (mass/volume) glucose
  (D) Pure distilled water

- **Marking Scheme & Solution:**
  - $0.9\%$ (mass/volume) sodium chloride solution is isotonic with human blood cells. Correct option is (A). — **[1 M]**

- **Why this Tier:** Practical NCERT medical application item recurring in CBSE examinations.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.5\b", t_low) or "reverse osmosis" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Reverse osmosis mechanism and membrane filtration
- **Question:**
  In reverse osmosis for seawater desalination, pure water moves from the saline solution side to the fresh water side across a semipermeable membrane when:
  (A) A hydrostatic pressure greater than the osmotic pressure is applied to the solution
  (B) Atmospheric pressure drops below vapor pressure
  (C) The solution is heated to its critical point
  (D) Solute particles pass through the cellulose acetate pores

- **Marking Scheme & Solution:**
  - Applying pressure $P > \Pi$ reverses the natural direction of osmosis, driving pure solvent through the membrane. Correct option is (A). — **[1 M]**

- **Why this Tier:** Practical NCERT industrial application of osmotic phenomena in Section A.
---"""
                return q.replace("__QNUM__", str(q_num))

            # Match 2-level section headings
            elif re.search(r"\b1\.1\b", t_low) or "types of" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Physical classification of solutions based on solute and solvent states
- **Question:**
  Camphor dissolved in pure nitrogen gas is classified as which type of solution according to NCERT?
  (A) Gaseous solution containing a solid solute
  (B) Solid solution containing a gaseous solute
  (C) Liquid solution containing a gaseous solute
  (D) Solid solution containing a liquid solute

- **Marking Scheme & Solution:**
  - Solvent is nitrogen gas and solute is solid camphor; hence it is a gaseous solution with solid solute. Correct option is (A). — **[1 M]**

- **Why this Tier:** Core classification staple directly from NCERT Table 1.1 in Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.2\b", t_low) or "concentration" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Temperature dependence of solution concentration units
- **Question:**
  Which of the following concentration units of a liquid solution is temperature-independent?
  (A) Molarity
  (B) Molality
  (C) Normality
  (D) Volume percentage ($V/V$)

- **Marking Scheme & Solution:**
  - Molality involves mass of solvent ($\text{kg}$), which does not change with temperature, unlike volume-dependent units. Correct option is (B). — **[1 M]**

- **Why this Tier:** Classical CBSE board benchmark on thermal invariance of concentration units.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.3\b", t_low) or "solubility" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Saturated solution and dissolution-crystallisation dynamic equilibrium
- **Question:**
  A solution in which no more solute can be dissolved at the same temperature and pressure is called:
  (A) Saturated solution, where rate of dissolution equals rate of crystallisation
  (B) Supersaturated solution, where solute precipitates spontaneously
  (C) Ideal solution, where solute-solvent interactions vanish
  (D) Unsaturated solution, where maximum vapour pressure is reached

- **Marking Scheme & Solution:**
  - In a saturated solution, a dynamic equilibrium exists: $\text{Solute} + \text{Solvent} \rightleftharpoons \text{Solution}$. Correct option is (A). — **[1 M]**

- **Why this Tier:** Fundamental definition and equilibrium benchmark in CBSE Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.4\b", t_low) or "vapour pressure of liquid solutions" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Equilibrium vapour pressure of pure liquids
- **Question:**
  At dynamic equilibrium in a closed vessel at constant temperature, the vapour pressure of a liquid is the pressure exerted by vapours when:
  (A) Rate of evaporation equals rate of condensation
  (B) Rate of boiling exceeds atmospheric pressure
  (C) All liquid completely transforms into vapor phase
  (D) Surface tension vanishes completely

- **Marking Scheme & Solution:**
  - Dynamic equilibrium between liquid and vapour phase yields a constant saturated vapour pressure at a given temperature. Correct option is (A). — **[1 M]**

- **Why this Tier:** Baseline physical definition governing liquid vapour pressures.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.5\b", t_low) or "ideal and non-ideal" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Thermodynamic criteria for ideal solutions
- **Question:**
  For an ideal solution obeying Raoult's law over the entire range of concentration, the enthalpy of mixing ($\Delta_{mix} H$) and volume of mixing ($\Delta_{mix} V$) are respectively:
  (A) $\Delta_{mix} H = 0$ and $\Delta_{mix} V = 0$
  (B) $\Delta_{mix} H > 0$ and $\Delta_{mix} V > 0$
  (C) $\Delta_{mix} H < 0$ and $\Delta_{mix} V < 0$
  (D) $\Delta_{mix} H = 0$ and $\Delta_{mix} V > 0$

- **Marking Scheme & Solution:**
  - An ideal solution requires zero enthalpy change and zero volume change on mixing pure components. Correct option is (A). — **[1 M]**

- **Why this Tier:** Fundamental thermodynamic criteria for solution ideality.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\b", t_low) or "colligative" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Fundamental definition of colligative properties
- **Question:**
  Colligative properties of a dilute solution depend exclusively on:
  (A) The number of solute particles present in the solution
  (B) The chemical nature and structure of the solute particles
  (C) The color and optical density of the solution
  (D) The atmospheric relative humidity

- **Marking Scheme & Solution:**
  - Colligative properties depend solely on the ratio of solute particles to solvent particles, irrespective of their chemical identity. Correct option is (A). — **[1 M]**

- **Why this Tier:** Foundational definition introducing Section 1.6.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.7\b", t_low) or "abnormal" in t_low:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Van 't Hoff factor ($i$) and colligative properties
- **Question:**
  If equimolar $0.1\text{ M}$ aqueous solutions of the following solutes are prepared, which one exhibits the highest boiling point elevation?
  (A) $\text{Al}_2(\text{SO}_4)_3$
  (B) $\text{NaCl}$
  (C) $\text{CaCl}_2$
  (D) Glucose

- **Marking Scheme & Solution:**
  - Elevation $\Delta T_b = i K_b m$. For $\text{Al}_2(\text{SO}_4)_3$, $i = 5$ particles, producing highest elevation. Correct option is (A). — **[1 M]**

- **Why this Tier:** Core quantitative staple testing van 't Hoff dissociation multiplier in Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Raoult's law for volatile and non-volatile liquid solutions
- **Question:**
  According to Raoult's law, the partial vapour pressure of any volatile component in a solution at a given temperature is equal to:
  (A) Its mole fraction multiplied by the vapour pressure of the pure component
  (B) Its molarity divided by atmospheric pressure
  (C) The sum of the mole fractions of all components
  (D) The square root of the solvent vapour pressure

- **Marking Scheme & Solution:**
  - Raoult's law states $p_i = p_i^0 x_i$. Correct option is (A). — **[1 M]**

- **Why this Tier:** Foundational governing principle of liquid solutions in CBSE Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
        else:
            q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Fundamental principle of __TOPIC__ (Item __INDEX__)
- **Question:**
  Based on the physical laws governing __TOPIC__, select the correct relation:
  (A) Directly proportional to governing parameter
  (B) Inversely proportional to governing parameter
  (C) Independent of external variation
  (D) Zero under standard equilibrium

- **Marking Scheme & Solution:**
  - Valid identification of the governing law and direct proportionality. Correct option is (A). — **[1 M]**

- **Why this Tier:** Core syllabus foundation.
---"""
            return q.replace("__QNUM__", str(q_num)).replace("__TOPIC__", topic_name).replace("__INDEX__", str(index + 1))

    elif category == "ar":
        if is_lenz or is_faraday:
            if index == 0:
                q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Lenz's Law and Conservation of Energy
- **Question:**
  Two statements are given — one labelled Assertion (A) and the other labelled Reason (R). Select the correct answer from the codes (a), (b), (c) and (d):
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** When a magnet is pushed towards a stationary conducting loop, an external mechanical force is required to keep it moving at constant velocity.
  **Reason (R):** According to Lenz's law, the induced current produces a magnetic field that opposes the motion of the magnet, and external mechanical work done converts into electrical energy.

- **Marking Scheme & Solution:**
  - Induced magnetic polarity opposes approaching magnet, creating a repulsive force. Hence Assertion is True. — **[1/2 M]**
  - Reason provides the exact thermodynamic foundation (conservation of energy). Correct answer is (a). — **[1/2 M]**

- **Why this Tier:** Evaluates understanding of energy conservation in electromagnetic phenomena.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Non-conservative nature of induced electric fields
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** The line integral of an induced electric field $\oint \mathbf{E} \cdot d\mathbf{l}$ around a closed loop is non-zero.
  **Reason (R):** Induced electric fields are produced by time-varying magnetic fields and are non-conservative in nature.

- **Marking Scheme & Solution:**
  - Line integral equals $-d\Phi_B/dt \neq 0$. Both A and R are true, and R correctly explains A. Correct answer is (a). — **[1 M]**

- **Why this Tier:** Advanced conceptual differentiation in CBSE Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_eddy:
            q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Lamination of magnetic cores and eddy current damping
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** In a dead-beat galvanometer, the coil former is deliberately made of a conducting metallic material.
  **Reason (R):** Eddy currents induced in a metallic former provide electromagnetic damping that brings the pointer rapidly to rest without prolonged oscillation.

- **Marking Scheme & Solution:**
  - Metallic former enables circulating eddy currents whose Lorentz force swiftly arrests coil swing. Both A and R are true, and R is correct explanation of A. Correct answer is (a). — **[1 M]**

- **Why this Tier:** Conceptual differentiator identifying damping mechanism in electrical instruments.
---"""
            return q.replace("__QNUM__", str(q_num))
        elif is_chem:
            if re.search(r"\b1\.7\b", t_low) or "abnormal" in t_low:
                q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Dimerization of carboxylic acids in non-polar solvents
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** The experimental molecular mass of ethanoic acid determined in benzene by freezing point depression is approximately $120\text{ g mol}^{-1}$ instead of $60\text{ g mol}^{-1}$.
  **Reason (R):** Ethanoic acid undergoes dimerization in benzene through intermolecular hydrogen bonding, halving the number of solute particles ($i \approx 0.5$).

- **Marking Scheme & Solution:**
  - Dimerization doubles effective molar mass and halves particles, giving $i \approx 0.5$. Both A and R are true, and R correctly explains A. Correct option is (a). — **[1 M]**

- **Why this Tier:** High-frequency board question on association-induced abnormal molecular mass.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.5\b", t_low) or "ideal" in t_low:
                q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Intermolecular interactions in non-ideal solutions
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** Mixing ethanol and acetone results in an increase in total solution volume and is an endothermic process ($\Delta_{mix} H > 0$).
  **Reason (R):** Acetone molecules insert between ethanol molecules, breaking existing hydrogen bonds and weakening solute-solvent interactions ($A-B < A-A, B-B$).

- **Marking Scheme & Solution:**
  - Weakening of intermolecular forces causes positive deviation, volume expansion, and heat absorption. Both A and R are true, and R correctly explains A. Correct option is (a). — **[1 M]**

- **Why this Tier:** Classical Section A conceptual differentiator evaluating intermolecular thermodynamics.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\b", t_low) or "osmosis" in t_low or "colligative" in t_low:
                q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Molar mass determination of polymers via osmotic pressure
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** Osmotic pressure is the most suitable colligative property for determining the molar masses of proteins, polymers, and biomolecules.
  **Reason (R):** Biomolecules are generally unstable at higher temperatures and their freezing point depressions are too minute to be measured accurately.

- **Marking Scheme & Solution:**
  - Osmotic pressure can be measured at room temperature with high sensitivity even for dilute solutions of high-mass macromolecules. Correct option is (a). — **[1 M]**

- **Why this Tier:** NCERT reasoning question recurring in CBSE Class 12 Chemistry Section A.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.3\b", t_low) or "henry" in t_low or "gas" in t_low:
                q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Temperature effect on gas solubility and aquatic comfort
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** Aquatic species are more comfortable in cold waters than in warm waters.
  **Reason (R):** The solubility of oxygen in water increases as temperature decreases due to the decrease in Henry's constant $K_H$.

- **Marking Scheme & Solution:**
  - Dissolution of gas is exothermic; lower temperature increases dissolved $O_2$ availability. Both A and R are true, and R explains A. Correct option is (a). — **[1 M]**

- **Why this Tier:** Classical NCERT Section 1.3.2 conceptual problem.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Colligative nature of solution properties
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** A $0.1\text{ M}$ aqueous solution of glucose and a $0.1\text{ M}$ aqueous solution of urea have the same osmotic pressure at $298\text{ K}$.
  **Reason (R):** Both glucose and urea are non-electrolytes and produce the same number of solute particles per unit volume in solution.

- **Marking Scheme & Solution:**
  - Since $i=1$ for both non-electrolytes, $\Pi = C R T$ is identical. Both A and R are true, and R correctly explains A. Correct option is (a). — **[1 M]**

- **Why this Tier:** NCERT conceptual verification of colligative particle dependence.
---"""
                return q.replace("__QNUM__", str(q_num))
        else:
            q = r"""### Q__QNUM__ | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Theoretical validity for __TOPIC__
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** The principle of __TOPIC__ applies universally under steady state.
  **Reason (R):** Conservation principles restrict spontaneous divergence of energy.

- **Marking Scheme & Solution:**
  - Both statements are valid and Reason correctly justifies Assertion. Correct option is (a). — **[1 M]**

- **Why this Tier:** Conceptual differentiation for board examinations.
---"""
            return q.replace("__QNUM__", str(q_num)).replace("__TOPIC__", topic_name)

    elif category == "vsa":
        if is_faraday:
            if index == 0:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Magnetic flux linkage and induced EMF formula
- **Question:**
  State Faraday's law of electromagnetic induction. Express the magnitude of induced EMF mathematically for a coil containing $N$ closely wound turns.

- **Marking Scheme & Solution:**
  - Statement: The magnitude of the induced electromotive force in a circuit is directly proportional to the time rate of change of magnetic flux through the circuit. — **[1 M]**
  - Formula: $\mathcal{E} = -N \frac{d\Phi_B}{dt}$ where $N$ is turn count and $\Phi_B$ is flux. — **[1 M]**

- **Why this Tier:** Standard 2-mark law definition and formula requirement in Section B.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Definition and SI unit of magnetic flux
- **Question:**
  Define magnetic flux $\Phi_B$ linked with a surface. State its SI unit and write down the condition when the flux linked is zero in a non-zero magnetic field.

- **Marking Scheme & Solution:**
  - Definition: $\Phi_B = \mathbf{B} \cdot \mathbf{A} = BA\cos\theta$, SI unit is Weber ($\text{Wb}$) or $\text{T m}^2$. — **[1 M]**
  - Zero flux condition: When plane of coil is parallel to field lines (area vector normal is perpendicular, $\theta = 90^\circ$). — **[1 M]**

- **Why this Tier:** Foundational definition and boundary condition test in Section B.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_lenz:
            if index == 0:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Statement and physical significance of Lenz's Law
- **Question:**
  State Lenz's law. Why does a metallic ring feel warm when a bar magnet is repeatedly thrust into and pulled out of it?

- **Marking Scheme & Solution:**
  - Statement: The polarity of the induced EMF produces a current which opposes the change in magnetic flux that produces it. — **[1 M]**
  - Continuous movement requires external mechanical work against magnetic opposition, which is converted to electrical energy and dissipated as Joule heat ($I^2 R t$). — **[1 M]**

- **Why this Tier:** Essential conceptual explanation directly referenced in NCERT questions.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Polarity prediction of capacitor plates in induced loop
- **Question:**
  A bar magnet is moved towards a closed loop containing a parallel-plate capacitor. Predict which plate becomes positive and justify using Lenz's law.

- **Marking Scheme & Solution:**
  - Direction of induced current determined via Lenz's law to oppose magnet approach. — **[1 M]**
  - Charge accumulates on plate towards which induced current flows, creating positive polarity. — **[1 M]**

- **Why this Tier:** Frequent NCERT conceptual question tested in Section B.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_eddy:
            if index == 0:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Nature and mitigation of Eddy Currents
- **Question:**
  What are eddy currents? How are eddy current losses minimized in the magnetic cores of transformers?

- **Marking Scheme & Solution:**
  - Definition: Induced swirling loops of electric current set up in bulk conductors subjected to changing magnetic flux. — **[1 M]**
  - Mitigation: Using laminated magnetic cores insulated by lacquer coatings parallel to magnetic lines of force. — **[1 M]**

- **Why this Tier:** Core syllabus foundation testing physical definition and practical engineering remedy.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Electromagnetic damping in oscillating plates
- **Question:**
  Why does an oscillating solid metallic plate stop swinging much faster than a plate with slots cut into it when placed between magnet poles?

- **Marking Scheme & Solution:**
  - Solid plate allows large continuous eddy current loops, creating high opposing Lorentz force damping. — **[1 M]**
  - Slots break continuous conduction paths, increasing electrical resistance and reducing eddy damping significantly. — **[1 M]**

- **Why this Tier:** Classical CBSE demonstration question on geometry effects of eddy currents.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_chem:
            if re.search(r"\b1\.2\b", t_low) or "concentration" in t_low:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Definition of molality and temperature invariance
- **Question:**
  Define molality ($m$) of a solution. Why is molality preferred over molarity ($M$) in expressing solution concentrations when temperature varies?

- **Marking Scheme & Solution:**
  - Definition: Molality is the number of moles of solute dissolved per kilogram ($1000\text{ g}$) of solvent: $m = \frac{w_2 \times 1000}{M_2 \times w_1}$. — **[1 M]**
  - Preference: Molality depends solely on masses, which do not change with temperature, whereas molarity depends on solution volume, which expands/contracts with temperature. — **[1 M]**

- **Why this Tier:** Core NCERT 2-mark question appearing consistently in CBSE Section B.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.3\b", t_low) or "henry" in t_low or "solubility" in t_low:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Henry's law statement and biological/industrial application
- **Question:**
  State Henry's law. Write down its mathematical expression and mention two practical applications (in scuba diving or carbonated beverages).

- **Marking Scheme & Solution:**
  - Statement: The partial pressure of a gas in vapour phase ($p$) is directly proportional to the mole fraction of the gas ($x$) in the solution: $p = K_H x$. — **[1 M]**
  - Applications: (1) Sealing soft drinks under high pressure to increase $\text{CO}_2$ solubility; (2) Diluting scuba breathing gas with helium to prevent painful 'bends'. — **[1 M]**

- **Why this Tier:** Standard 2-mark law definition and application requirement in Section B.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.5\b", t_low) or "reverse osmosis" in t_low:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Reverse osmosis and synthetic membrane materials
- **Question:**
  What is reverse osmosis? State one commercially viable semipermeable membrane material used for seawater desalination.

- **Marking Scheme & Solution:**
  - Definition: The movement of solvent from a solution into pure solvent through a semipermeable membrane when external pressure greater than osmotic pressure is applied. — **[1 M]**
  - Membrane material: Cellulose acetate supported on a porous sheet. — **[1 M]**

- **Why this Tier:** Practical technological application benchmark in Section B.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.7\b", t_low) or "abnormal" in t_low:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Van 't Hoff factor definition and theoretical values
- **Question:**
  Define the van 't Hoff factor ($i$). What is the value of $i$ for a dilute aqueous solution of barium chloride ($\text{BaCl}_2$) assuming complete dissociation?

- **Marking Scheme & Solution:**
  - Definition: $i = \frac{\text{Observed colligative property}}{\text{Calculated colligative property}} = \frac{\text{Normal molar mass}}{\text{Abnormal molar mass}}$. — **[1 M]**
  - For $\text{BaCl}_2 \rightarrow \text{Ba}^{2+} + 2\text{Cl}^-$, total particles per formula unit $n = 3$, so $i = 3$. — **[1 M]**

- **Why this Tier:** Standard 2-mark diagnostic on electrolytic multiplier.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Raoult's law for volatile liquid-liquid solutions
- **Question:**
  State Raoult's law for a solution containing volatile liquids. Express total vapour pressure mathematically in terms of mole fraction of one component.

- **Marking Scheme & Solution:**
  - Statement: For a solution of volatile liquids, the partial vapour pressure of each component is directly proportional to its mole fraction: $p_A = p_A^0 x_A$ and $p_B = p_B^0 x_B$. — **[1 M]**
  - Formula: $P_{total} = p_A^0 + (p_B^0 - p_A^0)x_B$. — **[1 M]**

- **Why this Tier:** Fundamental law definition in Section B.
---"""
                return q.replace("__QNUM__", str(q_num))
        else:
            q = r"""### Q__QNUM__ | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Core definition in __TOPIC__
- **Question:**
  Define the primary quantity in __TOPIC__ and state its SI unit and physical significance.

- **Marking Scheme & Solution:**
  - Accurate definition of governing quantity. — **[1 M]**
  - Standard SI unit and physical significance. — **[1 M]**

- **Why this Tier:** Standard Section B definition item.
---"""
            return q.replace("__QNUM__", str(q_num)).replace("__TOPIC__", topic_name)

    elif category == "sa":
        if is_faraday:
            if index == 0:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Induced EMF calculation in a circular coil
- **Question:**
  A circular coil of 200 turns and radius $10\text{ cm}$ is placed perpendicular to a uniform magnetic field changing at a constant rate of $0.5\text{ T/s}$.
  (a) Calculate the magnetic flux linked with each turn when the magnetic field is $0.8\text{ T}$.
  (b) Determine the magnitude of the induced EMF in the coil.
  (c) If the electrical resistance of the coil is $4\,\Omega$, find the induced current.

- **Marking Scheme & Solution:**
  - (a) Area $A = \pi (0.1)^2 = 0.01\pi\text{ m}^2$. $\Phi_B = B A = 0.8 \times 0.01\pi = 0.0251\text{ Wb}$. — **[1 M]**
  - (b) Induced EMF $|\mathcal{E}| = N A \frac{dB}{dt} = 200 \times 0.01\pi \times 0.5 = \pi \approx 3.14\text{ V}$. — **[1 M]**
  - (c) Induced current $I = \frac{\mathcal{E}}{R} = \frac{3.14\text{ V}}{4\,\Omega} = 0.785\text{ A}$. — **[1 M]**

- **Why this Tier:** Recurring 3-mark multi-step numerical in CBSE Physics Section C.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Motional EMF in a rotating conducting rod
- **Question:**
  A metallic rod of length $l = 0.5\text{ m}$ with one end pivoted rotates with uniform angular velocity $\omega = 100\text{ rad/s}$ perpendicular to a uniform magnetic field $B = 0.2\text{ T}$.
  (a) Derive the expression for the EMF induced across the ends of the rod from first principles.
  (b) Calculate the numerical magnitude of the induced EMF.

- **Marking Scheme & Solution:**
  - (a) Integrating element $dr$ at distance $r$ moving at $v = \omega r$: $d\mathcal{E} = B v dr = B \omega r dr$. — **[1 M]**
  - $\mathcal{E} = \int_0^l B \omega r dr = \frac{1}{2} B \omega l^2$. — **[1 M]**
  - (b) $\mathcal{E} = \frac{1}{2} \times 0.2 \times 100 \times (0.5)^2 = 2.5\text{ V}$. — **[1 M]**

- **Why this Tier:** High-frequency board staple testing derivation and direct substitution.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_eddy:
            q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Working principle of electromagnetic damping in galvanometers
- **Question:**
  (a) Describe what happens when a swinging copper plate enters the magnetic field between the poles of a strong electromagnet.
  (b) How does this phenomenon explain the principle of a 'dead-beat' galvanometer?
  (c) Diagram Setup: Flat copper sheet oscillating as a pendulum between flat electromagnet pole faces.

- **Marking Scheme & Solution:**
  - (a) Swirling eddy currents are induced in the copper plate. By Lenz's law, the Lorentz force opposes the motion, causing rapid damping. — **[1 M]**
  - (b) In a dead-beat galvanometer, eddy currents induced in the metallic coil former swiftly bring the needle to rest without oscillation. — **[1 M]**
  - (c) Explaining conversion of kinetic energy into Joule heat during electromagnetic braking. — **[1 M]**

- **Why this Tier:** Canonical 3-mark conceptual question testing application of electromagnetic induction in measuring instruments.
---"""
            return q.replace("__QNUM__", str(q_num))
        elif is_chem:
            # Check 3-level subtopics first
            if re.search(r"\b1\.3\.2\b", t_low) or "solubility of a gas" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Quantitative application of Henry's Law in aqueous dissolution
- **Question:**
  If $\text{N}_2$ gas is bubbled through water at $293\text{ K}$, how many millimoles of $\text{N}_2$ gas would dissolve in $1\text{ litre}$ of water?
  (Given: partial pressure of $\text{N}_2 = 0.987\text{ bar}$, Henry's law constant $K_H$ for $\text{N}_2$ at $293\text{ K} = 76.48\text{ kbar}$).

- **Marking Scheme & Solution:**
  - Mole fraction $x = \frac{p}{K_H} = \frac{0.987\text{ bar}}{76480\text{ bar}} = 1.29 \times 10^{-5}$. — **[1 M]**
  - $1\text{ litre}$ of water contains $\frac{1000}{18} = 55.55\text{ mol}$ $\text{H}_2\text{O}$. — **[1/2 M]**
  - $n = x \times 55.55 = 1.29 \times 10^{-5} \times 55.55 = 7.16 \times 10^{-4}\text{ mol} = 0.716\text{ mmol}$. — **[1.5 M]**

- **Why this Tier:** High-order Section C numerical testing Henry's Law constant unit scaling.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.4\.1\b", t_low) or "liquid-liquid solutions" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Vapour pressure and vapour composition of volatile liquid mixtures
- **Question:**
  Vapour pressures of chloroform ($\text{CHCl}_3$) and dichloromethane ($\text{CH}_2\text{Cl}_2$) at $298\text{ K}$ are $200\text{ mm Hg}$ and $415\text{ mm Hg}$ respectively.
  (a) Calculate the total vapour pressure of the solution prepared by mixing $25.5\text{ g}$ of $\text{CHCl}_3$ and $40.0\text{ g}$ of $\text{CH}_2\text{Cl}_2$ at $298\text{ K}$.
  (b) Calculate the mole fraction of each component in vapour phase.
  (Molar masses: $\text{CHCl}_3 = 119.5\text{ g mol}^{-1}$, $\text{CH}_2\text{Cl}_2 = 85.0\text{ g mol}^{-1}$).

- **Marking Scheme & Solution:**
  - Moles: $n(\text{CH}_2\text{Cl}_2) = \frac{40}{85} = 0.47\text{ mol}$, $n(\text{CHCl}_3) = \frac{25.5}{119.5} = 0.213\text{ mol}$. Total $= 0.683\text{ mol}$. Mole fractions: $x(\text{CH}_2\text{Cl}_2) = 0.688$, $x(\text{CHCl}_3) = 0.312$. — **[1 M]**
  - (a) $p = p_1^0 x_1 + p_2^0 x_2 = (415 \times 0.688) + (200 \times 0.312) = 285.5 + 62.4 = 347.9\text{ mm Hg}$. — **[1 M]**
  - (b) In vapour phase: $y(\text{CH}_2\text{Cl}_2) = \frac{285.5}{347.9} = 0.82$, $y(\text{CHCl}_3) = \frac{62.4}{347.9} = 0.18$. — **[1 M]**

- **Why this Tier:** Canonical NCERT Example 1.5 testing Dalton's and Raoult's law linkages.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.4\.2\b", t_low) or "special case" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Raoult's Law as a special case of Henry's Law
- **Question:**
  (a) State Raoult's law for a solution containing volatile liquids and Henry's law for a solution of a gas in a liquid.
  (b) Show mathematically how Raoult's law can be regarded as a special case of Henry's law.
  (c) How does the proportionality constant differ in both cases?

- **Marking Scheme & Solution:**
  - (a) Raoult's law: $p_1 = p_1^0 x_1$. Henry's law: $p = K_H x$. — **[1 M]**
  - (b) Both laws state that the partial vapour pressure of the volatile component or gas is directly proportional to its mole fraction in solution. — **[1 M]**
  - (c) Raoult's law is a special case of Henry's law in which the proportionality constant $K_H$ becomes equal to the vapour pressure of the pure component $p_1^0$. — **[1 M]**

- **Why this Tier:** Core NCERT theoretical connection tested in CBSE Section C.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.1\b", t_low) or "relative lowering" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Relative lowering of vapour pressure and molar mass derivation
- **Question:**
  (a) State Raoult's law for a solution of a non-volatile solute in a volatile solvent.
  (b) Derive the relationship between relative lowering of vapour pressure and the molar mass of the non-volatile solute ($M_2$).
  (c) Why is relative lowering a colligative property?

- **Marking Scheme & Solution:**
  - (a) The relative lowering of vapour pressure is equal to the mole fraction of the solute: $\frac{p_1^0 - p_1}{p_1^0} = x_2$. — **[1 M]**
  - (b) For dilute solutions, $x_2 = \frac{n_2}{n_1 + n_2} \approx \frac{n_2}{n_1} = \frac{w_2 / M_2}{w_1 / M_1} = \frac{w_2 M_1}{M_2 w_1}$. — **[1 M]**
  - (c) It depends exclusively on the ratio of solute particles to solvent particles, not on solute chemical identity. — **[1 M]**

- **Why this Tier:** Core Section C theoretical derivation.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.2\b", t_low) or "elevation of boiling" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Elevation of boiling point calculation for non-volatile solute
- **Question:**
  $18\text{ g}$ of glucose ($\text{C}_6\text{H}_{12}\text{O}_6$) is dissolved in $1\text{ kg}$ of water in a saucepan.
  (a) Calculate the molality of the glucose solution.
  (b) At what temperature will the water boil at $1.013\text{ bar}$ atmospheric pressure?
  (Given: $K_b$ for water $= 0.52\text{ K kg mol}^{-1}$, boiling point of pure water $= 373.15\text{ K}$, molar mass of glucose $= 180\text{ g mol}^{-1}$).

- **Marking Scheme & Solution:**
  - (a) Moles of glucose $= \frac{18}{180} = 0.1\text{ mol}$. Molality $m = \frac{0.1\text{ mol}}{1\text{ kg}} = 0.1\text{ mol kg}^{-1}$. — **[1 M]**
  - (b) Boiling point elevation $\Delta T_b = K_b \times m = 0.52 \times 0.1 = 0.052\text{ K}$. — **[1 M]**
  - Boiling point of solution $T_b = T_b^0 + \Delta T_b = 373.15 + 0.052 = 373.202\text{ K}$ (or $100.052^\circ\text{C}$). — **[1 M]**

- **Why this Tier:** NCERT standard benchmark numerical in CBSE Chemistry Section C.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.3\b", t_low) or "depression of freezing" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Freezing point depression calculation of ethylene glycol solution
- **Question:**
  $45\text{ g}$ of ethylene glycol ($\text{C}_2\text{H}_6\text{O}_2$) is mixed with $600\text{ g}$ of water.
  (a) Calculate the depression of freezing point ($\Delta T_f$).
  (b) Calculate the freezing point of the solution.
  (Given: $K_f$ for water $= 1.86\text{ K kg mol}^{-1}$, molar mass of $\text{C}_2\text{H}_6\text{O}_2 = 62\text{ g mol}^{-1}$).

- **Marking Scheme & Solution:**
  - (a) Moles of ethylene glycol $= \frac{45}{62} = 0.7258\text{ mol}$. Molality $m = \frac{0.7258}{0.600} = 1.2097\text{ mol kg}^{-1}$. — **[1 M]**
  - $\Delta T_f = K_f \times m = 1.86 \times 1.2097 = 2.25\text{ K}$. — **[1 M]**
  - (b) Freezing point $T_f = T_f^0 - \Delta T_f = 273.15 - 2.25 = 270.90\text{ K}$ (or $-2.25^\circ\text{C}$). — **[1 M]**

- **Why this Tier:** Classical 3-mark multi-step numerical in CBSE Chemistry Section C.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.6\.4\b", t_low) or "osmosis and osmotic" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Molar mass determination of a protein via osmotic pressure
- **Question:**
  $200\text{ cm}^3$ of an aqueous solution of a protein contains $1.26\text{ g}$ of the protein. The osmotic pressure of this solution at $300\text{ K}$ is found to be $2.57 \times 10^{-3}\text{ bar}$. Calculate the molar mass of the protein. ($R = 0.083\text{ L bar mol}^{-1}\text{ K}^{-1}$).

- **Marking Scheme & Solution:**
  - Volume $V = 0.200\text{ L}$, $\Pi = 2.57 \times 10^{-3}\text{ bar}$, $w_2 = 1.26\text{ g}$, $T = 300\text{ K}$. — **[1/2 M]**
  - Formula: $\Pi = \frac{w_2 R T}{M_2 V} \implies M_2 = \frac{w_2 R T}{\Pi V}$. — **[1 M]**
  - Substitution: $M_2 = \frac{1.26 \times 0.083 \times 300}{2.57 \times 10^{-3} \times 0.200} = 61,038\text{ g mol}^{-1}$. — **[1.5 M]**

- **Why this Tier:** Standard 3-mark numerical from NCERT exercises recurring in Section C.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.7\b", t_low) or "abnormal" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Van 't Hoff factor and degree of dissociation of an electrolyte
- **Question:**
  A $0.5\%\text{ (w/w)}$ aqueous solution of potassium chloride ($\text{KCl}$) freezes at $-0.24^\circ\text{C}$. Calculate:
  (a) The van 't Hoff factor $i$.
  (b) The degree of dissociation $\alpha$ of $\text{KCl}$.
  ($K_f = 1.86\text{ K kg mol}^{-1}$, molar mass of $\text{KCl} = 74.5\text{ g mol}^{-1}$).

- **Marking Scheme & Solution:**
  - Calculated $\Delta T_f = K_f \times \frac{0.5 \times 1000}{74.5 \times 99.5} = 0.1255\text{ K}$. Observed $\Delta T_f = 0.24\text{ K}$. — **[1 M]**
  - Van 't Hoff factor $i = \frac{\text{Observed } \Delta T_f}{\text{Calculated } \Delta T_f} = \frac{0.24}{0.1255} \approx 1.91$. — **[1 M]**
  - $\text{KCl} \rightleftharpoons \text{K}^+ + \text{Cl}^-$ ($n=2$), $\alpha = \frac{i - 1}{n - 1} = \frac{1.91 - 1}{2 - 1} = 0.91$ (or $91\%$). — **[1 M]**

- **Why this Tier:** High-frequency Section C differentiator testing electrolytic dissociation.
---"""
                return q.replace("__QNUM__", str(q_num))

            # Match 2-level sections
            elif re.search(r"\b1\.2\b", t_low) or "concentration" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Mole fraction and mass percentage calculation
- **Question:**
  Calculate the mole fraction of ethylene glycol ($\text{C}_2\text{H}_6\text{O}_2$) in an aqueous solution containing $20\%$ of $\text{C}_2\text{H}_6\text{O}_2$ by mass.
  (Molar masses: $\text{C}_2\text{H}_6\text{O}_2 = 62\text{ g mol}^{-1}$, $\text{H}_2\text{O} = 18\text{ g mol}^{-1}$).

- **Marking Scheme & Solution:**
  - In $100\text{ g}$ solution: mass of glycol $= 20\text{ g}$, mass of water $= 80\text{ g}$. — **[1/2 M]**
  - Moles of glycol $n_2 = \frac{20}{62} = 0.322\text{ mol}$; moles of water $n_1 = \frac{80}{18} = 4.444\text{ mol}$. — **[1 M]**
  - Mole fraction $x_2 = \frac{n_2}{n_1 + n_2} = \frac{0.322}{4.444 + 0.322} = \frac{0.322}{4.766} \approx 0.068$. — **[1.5 M]**

- **Why this Tier:** Core quantitative textbook calculation recurring in CBSE Section C.
---"""
                return q.replace("__QNUM__", str(q_num))
            elif re.search(r"\b1\.5\b", t_low) or "ideal" in t_low:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Thermodynamic criteria for ideality and negative deviation causes
- **Question:**
  (a) What are ideal solutions? State the two essential thermodynamic conditions for an ideal solution.
  (b) Explain why a binary mixture of chloroform and acetone exhibits a negative deviation from Raoult's law.

- **Marking Scheme & Solution:**
  - (a) An ideal solution obeys Raoult's law over the entire range of concentrations. Conditions: $\Delta_{mix} H = 0$ and $\Delta_{mix} V = 0$. — **[1.5 M]**
  - (b) Chloroform and acetone form strong intermolecular hydrogen bonds ($A-B$ interactions are stronger than $A-A$ and $B-B$), reducing escaping tendency and lowering vapour pressure below Raoult's law predictions. — **[1.5 M]**

- **Why this Tier:** Canonical NCERT conceptual question tested consistently in Section C.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Detailed conceptual explanation in __TOPIC__
- **Question:**
  Explain the mechanism of __TOPIC__. Derive the governing proportionality and discuss one practical constraint.

- **Marking Scheme & Solution:**
  - Qualitative explanation of underlying mechanism. — **[1 M]**
  - Mathematical formulation and proportionality relation. — **[1 M]**
  - Discussion of practical limitation or boundary condition. — **[1 M]**

- **Why this Tier:** Benchmark Section C structured conceptual problem.
---"""
                return q.replace("__QNUM__", str(q_num)).replace("__TOPIC__", topic_name)
        else:
            q = r"""### Q__QNUM__ | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Detailed conceptual explanation in __TOPIC__
- **Question:**
  Explain the mechanism of __TOPIC__. Derive the governing proportionality and discuss one practical constraint.

- **Marking Scheme & Solution:**
  - Qualitative explanation of underlying mechanism. — **[1 M]**
  - Mathematical formulation and proportionality relation. — **[1 M]**
  - Discussion of practical limitation or boundary condition. — **[1 M]**

- **Why this Tier:** Benchmark Section C structured conceptual problem.
---"""
            return q.replace("__QNUM__", str(q_num)).replace("__TOPIC__", topic_name)

    elif category == "case":
        if is_chem:
            q = r"""### Q__QNUM__ | Case-Based / Source-Based | 4 Mark(s) | 🟡 Tier 2
- **Target Concept:** Colligative properties, Osmotic pressure, and Desalination
- **Question:**
  **Case Study Passage:**
  Colligative properties depend exclusively on the number of solute particles in solution and not on their chemical identity. Among colligative properties, osmotic pressure ($\Pi = i C R T$) plays a central role in biological homeostasis and pharmaceutical formulations. Normal human red blood cells maintain an isotonic dynamic equilibrium with $0.9\%$ (mass/volume) sodium chloride solution (normal saline). When intravenous fluids are administered, maintaining strict isotonicity is essential to prevent hemolysis or crenation of blood cells. Furthermore, applying an external hydrostatic pressure greater than osmotic pressure to a concentrated saline solution forces solvent water backwards through a semipermeable cellulose acetate membrane, a process termed reverse osmosis (RO) that underpins modern seawater desalination systems.

  Answer the following questions:
  (a) What happens to human red blood cells when placed in a hypertonic $1.5\%\text{ (w/v) NaCl}$ solution? Explain. **(1 Mark)**
  (b) Why is osmotic pressure measurement preferred over freezing point depression for determining the molar masses of biomolecules and synthetic polymers? **(1 Mark)**
  (c) A solution prepared by dissolving $1.0\text{ g}$ of a polymer in $450\text{ mL}$ of water at $37^\circ\text{C}$ exhibits an osmotic pressure of $3.12 \times 10^{-4}\text{ atm}$. Calculate the molar mass of the polymer ($R = 0.0821\text{ L atm mol}^{-1}\text{ K}^{-1}$). **(2 Marks)**
  **OR**
  (c) Explain why equimolar sodium chloride ($\text{NaCl}$) exerts nearly double the osmotic pressure compared to a glucose solution of the same concentration. Calculate the van 't Hoff factor $i$ assuming $95\%$ dissociation. **(2 Marks)**

- **Marking Scheme & Solution:**
  - (a) Water diffuses out of the red blood cells by exosmosis into the hypertonic medium, causing the cells to shrink and crenate. — **[1 M]**
  - (b) Osmotic pressure can be measured at room temperature and produces large, accurately measurable heads even for extremely dilute polymer solutions. — **[1 M]**
  - (c) $M_2 = \frac{w_2 R T}{\Pi V} = \frac{1.0 \times 0.0821 \times 310.15}{3.12 \times 10^{-4} \times 0.450} \approx 181,200\text{ g mol}^{-1}$. — **[2 M]**
  - **OR** (c) $\text{NaCl}$ dissociates into two ions ($\text{Na}^+$ and $\text{Cl}^-$) doubling solute particles; $i = 1 + \alpha(2-1) = 1 + 0.95 = 1.95$. — **[2 M]**

- **Why this Tier:** Authentic CBSE Section E competency-based assessment evaluating medical and industrial colligative applications.
---"""
            return q.replace("__QNUM__", str(q_num))
        else:
            q = r"""### Q__QNUM__ | Case-Based / Source-Based | 4 Mark(s) | 🟡 Tier 2
- **Target Concept:** Electromagnetic Induction in industrial braking and power conversion
- **Question:**
  **Case Study Passage:**
  Michael Faraday's discovery of electromagnetic induction established that any change in magnetic flux through a circuit induces an electromotive force. Soon after, Heinrich Lenz demonstrated that the polarity of this induced EMF invariably opposes the change producing it, maintaining strict adherence to the law of conservation of energy. In bulk conductors, these induced currents form closed swirling loops called eddy currents. While eddy currents cause significant power dissipation in transformer cores, they are cleverly exploited in dead-beat moving coil galvanometers and frictionless electromagnetic brakes for high-speed bullet trains.

  Answer the following questions:
  (a) Write the mathematical relationship between induced EMF $\mathcal{E}$ and magnetic flux $\Phi_B$ for a coil of $N$ turns. **(1 Mark)**
  (b) Why does a metal disc dropped between the pole pieces of an electromagnet decelerate rapidly when current is turned on? **(1 Mark)**
  (c) A circular coil of area $0.05\text{ m}^2$ with 100 turns rotates in a uniform field of $0.2\text{ T}$ at $50\text{ rev/s}$. Calculate the peak EMF generated. **(2 Marks)**
  **OR**
  (c) Explain how lamination of a soft iron core minimizes eddy current heat dissipation, stating the quantitative dependence on lamination thickness $t$. **(2 Marks)**

- **Marking Scheme & Solution:**
  - (a) $\mathcal{E} = -N \frac{d\Phi_B}{dt}$. — **[1 M]**
  - (b) Eddy currents induced in the moving disc create opposing magnetic force (Lenz's Law), dissipating kinetic energy as heat. — **[1 M]**
  - (c) $\mathcal{E}_0 = N B A \omega = 100 \times 0.2 \times 0.05 \times (2\pi \times 50) = 100 \times 0.2 \times 0.05 \times 314.16 = 314.16\text{ V}$. — **[2 M]**
  - **OR** (c) Lamination creates insulating boundaries reducing eddy loop paths; power loss is proportional to $t^2$, where $t$ is thickness of laminations. — **[2 M]**

- **Why this Tier:** Authentic CBSE Section E competency-based assessment evaluating real-world application of induction.
---"""
            return q.replace("__QNUM__", str(q_num))

    elif category == "la":
        if is_chem:
            if re.search(r"\b1\.7\b", t_low) or "abnormal" in t_low:
                q = r"""### Q__QNUM__ | Long Answer (LA) / Derivation | 5 Mark(s) | 🔴 Tier 1
- **Target Concept:** Van 't Hoff factor derivation, molecular association, and abnormal molar mass
- **Question:**
  (a) Derive the relationship between the van 't Hoff factor ($i$) and degree of dissociation ($\alpha$) for a general electrolyte $A_x B_y \rightleftharpoons x A^{y+} + y B^{x-}$ producing $n = x + y$ ions.
  
  (b) Acetic acid ($\text{CH}_3\text{COOH}$) dissolved in benzene exhibits an apparent molar mass of $120\text{ g mol}^{-1}$. Explain this phenomenon with a neat structural formula showing the hydrogen-bonded dimer.
  
  (c) Determine the freezing point of an aqueous solution containing $1.74\text{ g}$ of potassium sulphate ($\text{K}_2\text{SO}_4$) dissolved in $100\text{ g}$ of water, assuming $90\%$ complete dissociation.
  ($K_f$ for water $= 1.86\text{ K kg mol}^{-1}$, molar mass of $\text{K}_2\text{SO}_4 = 174\text{ g mol}^{-1}$, freezing point of pure water $= 273.15\text{ K}$).

- **Marking Scheme & Solution:**
  - (a) Initial moles: $1$. Equilibrium moles: $1 - \alpha + n\alpha = 1 + (n - 1)\alpha$. Van 't Hoff factor $i = \frac{1 + (n-1)\alpha}{1} \implies \alpha = \frac{i - 1}{n - 1}$. — **[1.5 M]**
  - (b) Acetic acid forms a cyclic dimer in non-polar benzene via two intermolecular hydrogen bonds, halving the effective number of particles and doubling the apparent molecular mass from $60$ to $120\text{ g mol}^{-1}$. — **[1.5 M]**
  - (c) Dissociation: $\text{K}_2\text{SO}_4 \rightarrow 2\text{K}^+ + \text{SO}_4^{2-}$ ($n = 3$). $i = 1 + (3 - 1)(0.90) = 1 + 1.80 = 2.80$. — **[1/2 M]**
  - Molality $m = \frac{1.74 \times 1000}{174 \times 100} = 0.10\text{ mol kg}^{-1}$. — **[1/2 M]**
  - $\Delta T_f = i K_f m = 2.80 \times 1.86 \times 0.10 = 0.521\text{ K}$. Freezing point $T_f = 273.15 - 0.521 = 272.629\text{ K}$ (or $-0.521^\circ\text{C}$). — **[1 M]**

- **Why this Tier:** Pinnacle 5-mark comprehensive problem testing derivation, structural bonding, and quantitative colligative calculation in CBSE Section D.
---"""
                return q.replace("__QNUM__", str(q_num))
            else:
                q = r"""### Q__QNUM__ | Long Answer (LA) / Derivation | 5 Mark(s) | 🔴 Tier 1
- **Target Concept:** Derivation of boiling point elevation and molar mass calculation
- **Question:**
  (a) Derive the relationship between elevation of boiling point ($\Delta T_b$) and the molar mass ($M_2$) of a non-volatile solute from first principles:
  $$\Delta T_b = K_b \cdot \frac{w_2 \times 1000}{M_2 \times w_1}$$
  
  (b) Draw a neat vapour pressure vs temperature curve illustrating the boiling point of pure solvent and the solution, marking the boiling point elevation $\Delta T_b$.
  
  (c) $18\text{ g}$ of glucose ($\text{C}_6\text{H}_{12}\text{O}_6$) is dissolved in $1\text{ kg}$ of water in a saucepan. At what temperature will the water boil at $1.013\text{ bar}$? ($K_b$ for water $= 0.52\text{ K kg mol}^{-1}$, boiling point of pure water $= 373.15\text{ K}$).

- **Marking Scheme & Solution:**
  - (a) For dilute solutions, vapour pressure lowering $\Delta p \propto x_2$. Since $\Delta T_b \propto \Delta p$, $\Delta T_b \propto x_2 = \frac{w_2 / M_2}{w_1 / M_1}$. — **[1 M]**
  - Grouping solvent constants gives $\Delta T_b = K_b \times m = K_b \frac{w_2 \times 1000}{M_2 \times w_1}$. — **[1 M]**
  - (b) Diagram Setup: Smooth curves showing solution vapour pressure curve lying below pure solvent curve, indicating higher temperature to reach $1.013\text{ bar}$. — **[1 M]**
  - (c) Moles of glucose $= \frac{18}{180} = 0.1\text{ mol}$. Molality $m = \frac{0.1\text{ mol}}{1\text{ kg}} = 0.1\text{ mol kg}^{-1}$. — **[1 M]**
  - $\Delta T_b = 0.52 \times 0.1 = 0.052\text{ K}$. Boiling point $T_b = 373.15 + 0.052 = 373.202\text{ K}$ (or $100.052^\circ\text{C}$). — **[1 M]**

- **Why this Tier:** Canonical 5-mark board derivation and calculation in CBSE Class 12 Chemistry Section D.
---"""
                return q.replace("__QNUM__", str(q_num))
        elif is_faraday or "physics" in s_low:
            q = r"""### Q__QNUM__ | Long Answer (LA) / Derivation | 5 Mark(s) | 🔴 Tier 1
- **Target Concept:** First-principles derivation of alternating EMF induced in a rotating coil
- **Question:**
  (a) A rectangular coil of $N$ turns, each of area $A$, rotates with a constant angular velocity $\omega$ about an axis perpendicular to a uniform magnetic field $\mathbf{B}$. Derive the expression for the instantaneous electromotive force:
  $$\mathcal{E} = N B A \omega \sin(\omega t)$$
  
  (b) Diagram Setup: Rectangular coil $PQRS$ of dimensions $l \times b$ rotating about axis $OO'$ perpendicular to uniform magnetic field lines $\mathbf{B}$.
  
  (c) State the position of the coil relative to the magnetic field lines when the induced EMF is (i) zero, and (ii) maximum.

- **Marking Scheme & Solution:**
  - (a) Magnetic flux at time $t$: $\Phi_B = \mathbf{B} \cdot \mathbf{A} = B A \cos(\omega t)$. — **[1 M]**
  - Applying Faraday's law: $\mathcal{E} = -N \frac{d\Phi_B}{dt} = -N \frac{d}{dt}[B A \cos(\omega t)]$. — **[1 M]**
  - Differentiation step: $\frac{d}{dt}[\cos(\omega t)] = -\omega \sin(\omega t) \implies \mathcal{E} = N B A \omega \sin(\omega t)$. — **[1 M]**
  - (b) Neat schematic showing rotating coil axis and angle $\theta = \omega t$. — **[1 M]**
  - (c) (i) $\mathcal{E} = 0$ when coil plane is perpendicular to field ($\omega t = 0^\circ$ or $180^\circ$). (ii) $\mathcal{E} = \mathcal{E}_{max}$ when coil plane is parallel to field ($\omega t = 90^\circ$ or $270^\circ$). — **[1 M]**

- **Why this Tier:** Canonical 5-mark board derivation featuring in CBSE Class 12 Physics Section D.
---"""
            return q.replace("__QNUM__", str(q_num))
        else:
            q = r"""### Q__QNUM__ | Long Answer (LA) / Derivation | 5 Mark(s) | 🔴 Tier 1
- **Target Concept:** First-principles derivation for __TOPIC__
- **Question:**
  (a) Starting from fundamental definitions, derive the comprehensive governing relation for __TOPIC__.
  (b) Provide the schematic diagram setup.
  (c) Evaluate the asymptotic limits under extreme parameter conditions.

- **Marking Scheme & Solution:**
  - (a) Numbered steps of derivation from first principles. — **[2 M]**
  - (b) Accurate diagrammatic representation. — **[1 M]**
  - (c) Limit evaluation and physical interpretation. — **[2 M]**

- **Why this Tier:** Core comprehensive 5-mark examination problem.
---"""
            return q.replace("__QNUM__", str(q_num)).replace("__TOPIC__", topic_name)

    # Ultimate fallback
    q = r"""### Q__QNUM__ | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Core concept in __TOPIC__
- **Question:**
  Identify the primary physical law governing __TOPIC__.
  (A) Option A
  (B) Option B
  (C) Option C
  (D) Option D

- **Marking Scheme & Solution:**
  - Correct selection. — **[1 M]**

- **Why this Tier:** Core syllabus foundation.
---"""
    return q.replace("__QNUM__", str(q_num)).replace("__TOPIC__", topic_name)


def mock_generate_cbse_questions(
    topic_name: str,
    latex_chunk: str,
    requested_selection: str,
    subject: str = "Physics",
    grade: str = "12",
    chapter_title: str = "Electromagnetic Induction",
    topic_weight: Optional[float] = None,
    allocated_quota: Optional[int] = None,
    start_q_index: int = 1,
) -> str:
    """Offline heuristic generator producing valid CBSE-formatted examination questions matching the template."""
    if allocated_quota is not None and allocated_quota <= 0:
        return ""

    category_counts = parse_selection_counts(requested_selection, allocated_quota)

    outputs: List[str] = []
    current_q = start_q_index

    for cat, count in category_counts:
        for idx in range(count):
            q_str = _build_mock_question(
                topic_name=topic_name,
                latex_chunk=latex_chunk,
                subject=subject,
                grade=grade,
                chapter_title=chapter_title,
                category=cat,
                index=idx,
                q_num=current_q,
            )
            outputs.append(q_str)
            current_q += 1

    return "\n".join(outputs).strip()


async def fetch_prompt_template(
    base_url: str = "http://localhost:3000",
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Fetches the CBSE prompt template from the CBSE Generator API (/api/prompt-template).
    Falls back gracefully to the embedded template if the API is offline.
    """
    clean_url = (base_url or "http://localhost:3000").rstrip("/")
    if clean_url.endswith("/api/prompt-template") or clean_url.endswith("/prompt-template"):
        endpoint = clean_url
    else:
        endpoint = f"{clean_url}/api/prompt-template"

    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=3.0)
        close_client = True

    host_key = re.sub(r"^https?://", "", endpoint).split("/")[0]
    if host_key in _OFFLINE_HOSTS:
        return {
            "system_prompt": CBSE_SYSTEM_PROMPT,
            "user_prompt_template": CBSE_USER_PROMPT_TEMPLATE,
            "input_contract": [
                "subject", "grade", "chapter_title", "topic_name",
                "topic_weight", "allocated_quota", "latex_chunk", "requested_selection"
            ],
            "source": "embedded_fallback",
        }

    try:
        res = await client.get(endpoint, timeout=httpx.Timeout(3.0, connect=FAST_CONNECT_TIMEOUT))
        res.raise_for_status()
        data = res.json()
        if isinstance(data, dict) and data.get("success"):
            return {
                "system_prompt": data.get("systemPrompt", CBSE_SYSTEM_PROMPT),
                "user_prompt_template": data.get("userPromptTemplate", CBSE_USER_PROMPT_TEMPLATE),
                "input_contract": data.get("inputContract", [
                    "subject", "grade", "chapter_title", "topic_name",
                    "topic_weight", "allocated_quota", "latex_chunk", "requested_selection"
                ]),
                "source": "remote_api",
            }
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _OFFLINE_HOSTS.add(host_key)
    except Exception:
        pass
    finally:
        if close_client:
            await client.aclose()

    return {
        "system_prompt": CBSE_SYSTEM_PROMPT,
        "user_prompt_template": CBSE_USER_PROMPT_TEMPLATE,
        "input_contract": [
            "subject", "grade", "chapter_title", "topic_name",
            "topic_weight", "allocated_quota", "latex_chunk", "requested_selection"
        ],
        "source": "embedded_fallback",
    }


async def generate_topic_questions(
    topic_name: str,
    latex_chunk: str,
    topic_weight: float,
    allocated_quota: int,
    difficulty_multiplier: float = 1.0,
    subject: str = "Physics",
    grade: str = "12",
    chapter_title: str = "Electromagnetic Induction",
    requested_selection: Optional[str] = None,
    system_prompt: Optional[str] = None,
    user_prompt_template: Optional[str] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    use_mock: bool = False,
    start_q_index: int = 1,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Generates questions for a single topic by routing topic weight and quota into the CBSE prompt template.
    Invokes CBSE generator API endpoint, Gemini API, or offline mock generator.
    """
    effective_system_prompt = system_prompt or CBSE_SYSTEM_PROMPT

    if requested_selection is None:
        requested_selection = build_requested_selection(
            allocated_quota=allocated_quota,
            difficulty_multiplier=difficulty_multiplier,
            final_weight=topic_weight,
        )

    user_prompt = render_cbse_user_prompt(
        subject=subject,
        grade=grade,
        chapter_title=chapter_title,
        topic_name=topic_name,
        topic_weight=topic_weight,
        allocated_quota=allocated_quota,
        latex_chunk=latex_chunk,
        requested_selection=requested_selection,
        template=user_prompt_template,
    )

    if allocated_quota <= 0:
        return {
            "topic_name": topic_name,
            "topic_weight": topic_weight,
            "allocated_quota": 0,
            "difficulty_multiplier": difficulty_multiplier,
            "requested_selection": requested_selection,
            "prompt": {
                "system_prompt": effective_system_prompt,
                "user_prompt": user_prompt,
            },
            "output": "",
            "mode": "skipped",
            "success": True,
        }

    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0)
        close_client = True

    try:
        # If generation via CBSE generator server requested
        if api_url and not use_mock:
            host_key = re.sub(r"^https?://", "", api_url).split("/")[0]
            if host_key not in _OFFLINE_HOSTS:
                try:
                    payload = {
                        "subject": subject,
                        "grade": grade,
                        "chapter_title": chapter_title,
                        "topic_name": topic_name,
                        "topic_weight": topic_weight,
                        "allocated_quota": allocated_quota,
                        "latex_chunk": latex_chunk,
                        "requested_selection": requested_selection,
                        "apiKey": api_key,
                        "useMock": use_mock,
                    }
                    res = await client.post(api_url, json=payload, timeout=httpx.Timeout(15.0, connect=FAST_CONNECT_TIMEOUT))
                    if res.status_code == 200:
                        data = res.json()
                        if data.get("success") and data.get("output"):
                            prompt_used = data.get("promptUsed") or {
                                "system_prompt": effective_system_prompt,
                                "user_prompt": user_prompt,
                            }
                            return {
                                "topic_name": topic_name,
                                "topic_weight": topic_weight,
                                "allocated_quota": allocated_quota,
                                "difficulty_multiplier": difficulty_multiplier,
                                "requested_selection": requested_selection,
                                "prompt": {
                                    "system_prompt": prompt_used.get("systemPrompt", effective_system_prompt),
                                    "user_prompt": prompt_used.get("userPrompt", user_prompt),
                                },
                                "output": data["output"],
                                "mode": data.get("mode", "live" if not use_mock else "mock"),
                                "source": "cbse_generator_api",
                                "success": True,
                            }
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    _OFFLINE_HOSTS.add(host_key)
                except Exception:
                    pass  # Fall through to direct Gemini or mock

        # If direct Gemini API key available and live mode requested
        effective_key = api_key or os.getenv("GEMINI_API_KEY") or DEFAULT_GEMINI_API_KEY
        if not use_mock and effective_key:
            candidate_models = [
                os.getenv("GEMINI_MODEL"),
                "gemini-flash-lite-latest",
                "gemini-3.5-flash-lite",
                "gemini-3.5-flash",
                "gemini-3.7-flash",
                "gemini-3.1-flash-lite",
                "gemini-flash-latest",
                "gemini-3.8-flash",
                "gemini-2.5-flash",
                "gemini-1.5-flash",
            ]
            candidate_models = [m for m in candidate_models if m]
            for model_name in candidate_models:
                try:
                    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={effective_key}"
                    body = {
                        "system_instruction": {"parts": [{"text": effective_system_prompt}]},
                        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                        "generationConfig": {"temperature": 0.2, "topP": 0.95},
                    }
                    g_res = await client.post(gemini_url, json=body, timeout=httpx.Timeout(30.0, connect=10.0))
                    if g_res.status_code == 200:
                        g_data = g_res.json()
                        gen_text = g_data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        if gen_text.strip():
                            return {
                                "topic_name": topic_name,
                                "topic_weight": topic_weight,
                                "allocated_quota": allocated_quota,
                                "difficulty_multiplier": difficulty_multiplier,
                                "requested_selection": requested_selection,
                                "prompt": {
                                    "system_prompt": effective_system_prompt,
                                    "user_prompt": user_prompt,
                                },
                                "output": gen_text.strip(),
                                "mode": "live",
                                "source": "direct_gemini",
                                "model": model_name,
                                "success": True,
                            }
                except Exception:
                    continue

        # Offline Mock Generator
        mock_output = mock_generate_cbse_questions(
            topic_name=topic_name,
            latex_chunk=latex_chunk,
            requested_selection=requested_selection,
            subject=subject,
            grade=grade,
            chapter_title=chapter_title,
            topic_weight=topic_weight,
            allocated_quota=allocated_quota,
            start_q_index=start_q_index,
        )

        return {
            "topic_name": topic_name,
            "topic_weight": topic_weight,
            "allocated_quota": allocated_quota,
            "difficulty_multiplier": difficulty_multiplier,
            "requested_selection": requested_selection,
            "prompt": {
                "system_prompt": effective_system_prompt,
                "user_prompt": user_prompt,
            },
            "output": mock_output,
            "mode": "mock",
            "source": "offline_mock",
            "success": True,
        }

    finally:
        if close_client:
            await client.aclose()


async def generate_chapter_questions(
    manifest: Dict[str, Any],
    chunks: List[Any],
    subject: str = "Physics",
    grade: str = "12",
    chapter_title: str = "Electromagnetic Induction",
    prompt_template: Optional[Dict[str, Any]] = None,
    api_url: Optional[str] = None,
    api_key: Optional[str] = None,
    use_mock: bool = False,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Takes a synthesized chapter manifest and raw chunks, routes each topic's weight and quota into the
    CBSE question generator prompt template, and generates a unified, sequentially-numbered examination paper.
    """
    chunks_data = normalize_chunks(chunks)
    topics_list = manifest.get("topics", [])
    toc_topic_names = [t["topic_name"] for t in topics_list]

    topic_texts = align_chunks_to_topics(chunks_data, toc_topic_names)

    # Ingest prompt template components
    effective_template = prompt_template or {
        "system_prompt": CBSE_SYSTEM_PROMPT,
        "user_prompt_template": CBSE_USER_PROMPT_TEMPLATE,
        "source": "embedded_fallback",
    }
    sys_prompt = effective_template.get("system_prompt", CBSE_SYSTEM_PROMPT)
    user_template = effective_template.get("user_prompt_template", CBSE_USER_PROMPT_TEMPLATE)

    effective_subject = manifest.get("subject") or subject
    effective_grade = manifest.get("grade") or grade
    effective_chapter = manifest.get("chapter_title") or chapter_title

    # Resolve dual mode:
    # 1. Explicit dynamic requested (is_dynamic_target=True or target_questions in ('auto', 'dynamic'))
    # 2. Manual target questions specified in manifest (integer within [10, 50], quotas apportioned dynamically)
    # 3. Default: If topics lack allocated_quota and no manual target, budget dynamically.
    raw_target = manifest.get("target_questions")
    is_explicit_dyn = (
        manifest.get("is_dynamic_target") is True
        or (isinstance(raw_target, str) and raw_target.strip().lower() in ("auto", "dynamic"))
    )

    if is_explicit_dyn:
        dyn_target = compute_dynamic_target_questions(manifest)
        topics_list = apportion_quotas(topics_list, dyn_target)
        manifest["topics"] = topics_list
        manifest["target_questions"] = dyn_target
        manifest["is_dynamic_target"] = True
        manifest["target_questions_mode"] = "dynamic"
    elif raw_target is not None and str(raw_target).strip() != "":
        val_target, is_dyn = validate_target_questions(raw_target, clamp=True)
        if is_dyn or val_target is None:
            dyn_target = compute_dynamic_target_questions(manifest)
            topics_list = apportion_quotas(topics_list, dyn_target)
            manifest["topics"] = topics_list
            manifest["target_questions"] = dyn_target
            manifest["is_dynamic_target"] = True
            manifest["target_questions_mode"] = "dynamic"
        else:
            if all("allocated_quota" not in t for t in topics_list) or any(t.get("allocated_quota") is None for t in topics_list):
                topics_list = apportion_quotas(topics_list, val_target)
                manifest["topics"] = topics_list
            manifest["target_questions"] = val_target
            manifest["is_dynamic_target"] = False
            manifest["target_questions_mode"] = "manual"
    elif len(topics_list) > 0 and all("allocated_quota" not in t for t in topics_list):
        dyn_target = compute_dynamic_target_questions(manifest)
        topics_list = apportion_quotas(topics_list, dyn_target)
        manifest["topics"] = topics_list
        manifest["target_questions"] = dyn_target
        manifest["is_dynamic_target"] = True
        manifest["target_questions_mode"] = "dynamic"

    topic_results = []
    target_q = manifest.get("target_questions", sum(t.get("allocated_quota", 0) for t in topics_list))
    combined_exam_lines = [
        f"# CENTRAL BOARD OF SECONDARY EDUCATION",
        f"## SENIOR SCHOOL CERTIFICATE EXAMINATION (CLASS {effective_grade})",
        f"### SUBJECT: {effective_subject.upper()} | CHAPTER: {effective_chapter.upper()}",
        f"**Target Questions:** {target_q}",
        f"**General Instructions:** Questions are apportioned in accordance with topic difficulty and syllabus weights.",
        f"\n---\n",
    ]

    current_q_index = 1
    total_generated = 0

    for topic_info in topics_list:
        topic_name = topic_info["topic_name"]
        quota = topic_info.get("allocated_quota", 1)
        if quota <= 0:
            continue

        weight = topic_info.get("final_weight", 0.0)
        diff = topic_info.get("difficulty_multiplier", 1.0)
        chunk_text = topic_texts.get(topic_name, "")

        topic_res = await generate_topic_questions(
            topic_name=topic_name,
            latex_chunk=chunk_text,
            topic_weight=weight,
            allocated_quota=quota,
            difficulty_multiplier=diff,
            subject=effective_subject,
            grade=effective_grade,
            chapter_title=effective_chapter,
            system_prompt=sys_prompt,
            user_prompt_template=user_template,
            api_url=api_url,
            api_key=api_key,
            use_mock=use_mock,
            start_q_index=current_q_index,
            client=client,
        )

        raw_output = topic_res["output"]
        # Ensure sequential question numbering across topics
        q_matches = list(re.finditer(r"###\s*Q(\d+)", raw_output))
        if q_matches:
            # If generator produced more questions than allocated quota, trim to quota
            if len(q_matches) > quota:
                cutoff = q_matches[quota].start()
                last_div = raw_output[:cutoff].rfind("---")
                if last_div != -1 and raw_output[last_div:cutoff].strip() == "---":
                    cutoff = last_div
                raw_output = raw_output[:cutoff].strip()
                q_matches = list(re.finditer(r"###\s*Q(\d+)", raw_output))

            renumbered = raw_output
            # Renumber in reverse to avoid offset mismatch
            for idx, match in reversed(list(enumerate(q_matches))):
                assigned_num = current_q_index + idx
                start, end = match.span()
                replacement = f"### Q{assigned_num}"
                renumbered = renumbered[:start] + replacement + renumbered[end:]
            topic_res["output"] = renumbered
            current_q_index += len(q_matches)
            total_generated += len(q_matches)
        else:
            topic_res["output"] = raw_output

        topic_results.append(topic_res)
        combined_exam_lines.append(
            f"\n## Topic Section: {topic_name} (Syllabus Weight: {round(weight * 100, 2)}% | Allocated Quota: {quota})\n"
        )
        combined_exam_lines.append(topic_res["output"])
        combined_exam_lines.append("\n---\n")

    return {
        "chapter_title": effective_chapter,
        "subject": effective_subject,
        "grade": effective_grade,
        "manifest": manifest,
        "prompt_template": effective_template,
        "topics": topic_results,
        "total_questions_generated": total_generated,
        "combined_exam_markdown": "\n".join(combined_exam_lines),
    }


async def orchestrate_and_generate(
    chunks_endpoint: str = DEFAULT_CHUNKS_ENDPOINT,
    toc_endpoint: str = DEFAULT_TOC_ENDPOINT,
    questions_endpoint: str = DEFAULT_QUESTIONS_ENDPOINT,
    ollama_url: str = DEFAULT_OLLAMA_SYSTEMONE_URL,
    generate_url: Optional[str] = DEFAULT_CBSE_GENERATE_URL,
    prompt_template_url: Optional[str] = DEFAULT_CBSE_PROMPT_TEMPLATE_URL,
    prompt_template: Optional[Dict[str, Any]] = None,
    concurrency: int = 4,
    target_questions: Optional[Union[int, str]] = None,
    dynamic_target: Optional[bool] = None,
    min_questions: Optional[int] = None,
    max_questions: int = 50,
    subject: str = "Physics",
    grade: str = "12",
    chapter_title: str = "Electromagnetic Induction",
    mock_mode: bool = False,
    api_key: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    clamp_manual: bool = True,
) -> Dict[str, Any]:
    """
    End-to-End Orchestrator + CBSE Question Generator Pipeline:
    1. Ingests chapter chunks, TOC, and questions from endpoints.
    2. Runs Nimble passes to score question impact and topic difficulty.
    3. Synthesizes deterministic topic weights and quota allocations (dynamically or manually).
    4. Routes topic weights, quotas, and LaTeX chunks into CBSE prompt template.
    5. Generates authentic CBSE examination questions based on topic weights.
    6. Compiles a complete chapter examination paper.
    """
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=60.0)
        close_client = True

    try:
        if dynamic_target is True:
            is_dynamic = True
            resolved_target = "auto"
        else:
            val_target, is_dyn = validate_target_questions(target_questions, clamp=clamp_manual)
            if is_dyn:
                is_dynamic = True
                resolved_target = "auto"
            else:
                is_dynamic = False
                resolved_target = val_target

        # Step 1: Compute weights and quotas
        manifest = await compute_chapter_weights(
            chunks_endpoint=chunks_endpoint,
            toc_endpoint=toc_endpoint,
            questions_endpoint=questions_endpoint,
            ollama_url=ollama_url,
            concurrency=concurrency,
            target_questions=resolved_target,
            dynamic_target=is_dynamic,
            min_questions=min_questions,
            max_questions=max_questions,
            mock_mode=mock_mode,
            client=client,
            clamp_manual=clamp_manual,
        )

        # Step 2: Ingest CBSE Prompt Template
        if prompt_template is None:
            pt_url = prompt_template_url or DEFAULT_CBSE_PROMPT_TEMPLATE_URL
            prompt_template = await fetch_prompt_template(base_url=pt_url, client=client)

        # Step 3: Fetch chunks data for question generation
        chunks_raw = await _fetch_with_fallback(client, chunks_endpoint, "chunks")
        chunks = normalize_chunks(chunks_raw)

        # Step 4: Route weights, quotas, and chunks into CBSE generator prompt template
        exam_subject = manifest.get("subject") if (subject == "Physics" and manifest.get("subject")) else subject
        exam_grade = manifest.get("grade") if (grade == "12" and manifest.get("grade")) else grade
        exam_chapter = manifest.get("chapter_title") if (chapter_title == "Electromagnetic Induction" and manifest.get("chapter_title")) else chapter_title

        exam_result = await generate_chapter_questions(
            manifest=manifest,
            chunks=chunks,
            subject=exam_subject,
            grade=exam_grade,
            chapter_title=exam_chapter,
            prompt_template=prompt_template,
            api_url=generate_url,
            api_key=api_key,
            use_mock=mock_mode,
            client=client,
        )

        exam_result["prompt_template_source"] = prompt_template.get("source", "embedded_fallback")
        return exam_result

    finally:
        if close_client:
            await client.aclose()


# ----------------- CLI INTERFACE -----------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Chapter Topic Weight Orchestrator bridging Endpoints, Ollama /v1/systemone, and CBSE Question Generator"
    )
    parser.add_argument(
        "--chunks-url",
        default=DEFAULT_CHUNKS_ENDPOINT,
        help=f"URL or file path for Chunks endpoint (default: {DEFAULT_CHUNKS_ENDPOINT})",
    )
    parser.add_argument(
        "--toc-url",
        default=DEFAULT_TOC_ENDPOINT,
        help=f"URL or file path for TOC endpoint (default: {DEFAULT_TOC_ENDPOINT})",
    )
    parser.add_argument(
        "--questions-url",
        default=DEFAULT_QUESTIONS_ENDPOINT,
        help=f"URL or file path for Questions endpoint (default: {DEFAULT_QUESTIONS_ENDPOINT})",
    )
    parser.add_argument(
        "--chunks-file",
        default=None,
        help="Local file path for Chunks (.json or .tex)",
    )
    parser.add_argument(
        "--toc-file",
        default=None,
        help="Local file path for TOC JSON",
    )
    parser.add_argument(
        "--questions-file",
        default=None,
        help="Local file path for Questions JSON",
    )
    parser.add_argument(
        "--ollama-url",
        default=DEFAULT_OLLAMA_SYSTEMONE_URL,
        help=f"URL for Ollama /v1/systemone endpoint (default: {DEFAULT_OLLAMA_SYSTEMONE_URL})",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=4,
        help="Concurrency semaphore limit for Ollama requests (default: 4)",
    )
    def parse_target_questions_arg(val):
        if val is None:
            return "auto"
        v = str(val).strip().lower()
        if v in ("auto", "dynamic"):
            return "auto"
        try:
            num, is_dyn = validate_target_questions(val, clamp=False)
            if is_dyn or num is None:
                return "auto"
            return num
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"Invalid target-questions value: '{val}'. Expected an integer between {MIN_MANUAL_QUESTIONS} and {MAX_MANUAL_QUESTIONS} (inclusive), or 'auto'."
            )

    parser.add_argument(
        "--target-questions",
        type=parse_target_questions_arg,
        default="auto",
        help=f"Downstream target number of questions: integer between {MIN_MANUAL_QUESTIONS} and {MAX_MANUAL_QUESTIONS} for manual total, or 'auto' for fully dynamic (default: 'auto')",
    )
    parser.add_argument(
        "--dynamic-target",
        action="store_true",
        help="Calculate downstream target number of questions dynamically based on topic weights, raw impact, topic count, and difficulty density",
    )
    parser.add_argument(
        "--min-questions",
        type=int,
        default=None,
        help="Optional minimum lower bound for dynamic question target",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=50,
        help="Optional maximum upper bound for dynamic question target (default: 50)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in offline mock mode (intelligent heuristic simulation without live Ollama)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="File path to save the resulting chapter manifest JSON",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate CBSE examination questions using the prompt template based on computed topic weights",
    )
    parser.add_argument(
        "--generate-url",
        default=DEFAULT_CBSE_GENERATE_URL,
        help=f"URL for CBSE Question Generator /api/generate endpoint (default: {DEFAULT_CBSE_GENERATE_URL})",
    )
    parser.add_argument(
        "--prompt-template-url",
        default=DEFAULT_CBSE_PROMPT_TEMPLATE_URL,
        help=f"URL for CBSE Question Generator /api/prompt-template endpoint (default: {DEFAULT_CBSE_PROMPT_TEMPLATE_URL})",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("GEMINI_API_KEY") or DEFAULT_GEMINI_API_KEY,
        help="Optional Google Gemini API key for live LLM question generation",
    )
    parser.add_argument(
        "--subject",
        default="Physics",
        help="Examination subject (default: Physics)",
    )
    parser.add_argument(
        "--grade",
        default="12",
        help="Examination grade/class (default: 12)",
    )
    parser.add_argument(
        "--chapter-title",
        default="Electromagnetic Induction",
        help="Chapter title (default: Electromagnetic Induction)",
    )
    parser.add_argument(
        "--exam-output",
        default=None,
        help="File path to save the combined CBSE examination Markdown paper",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    chunks_src = args.chunks_file or args.chunks_url
    toc_src = args.toc_file or args.toc_url
    questions_src = args.questions_file or args.questions_url

    is_dynamic = args.dynamic_target or (str(args.target_questions or "").strip().lower() in ("auto", "dynamic"))

    try:
        if args.generate:
            target_q = "auto" if is_dynamic else args.target_questions
            print(f"Executing End-to-End Orchestrator + CBSE Question Generation for target_questions={target_q} (mode={'dynamic' if is_dynamic else 'manual'})...")
            result = asyncio.run(
                orchestrate_and_generate(
                    chunks_endpoint=chunks_src,
                    toc_endpoint=toc_src,
                    questions_endpoint=questions_src,
                    ollama_url=args.ollama_url,
                    generate_url=args.generate_url,
                    prompt_template_url=args.prompt_template_url,
                    concurrency=args.concurrency,
                    target_questions=target_q,
                    dynamic_target=is_dynamic,
                    min_questions=args.min_questions,
                    max_questions=args.max_questions,
                    subject=args.subject,
                    grade=args.grade,
                    chapter_title=args.chapter_title,
                    mock_mode=args.mock,
                    api_key=args.api_key,
                )
            )

            manifest_json = json.dumps(result["manifest"], indent=2)
            print("\n--- SYNTHESIZED CHAPTER MANIFEST ---")
            print(manifest_json)

            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(manifest_json)
                print(f"Manifest successfully written to {args.output}")

            if args.exam_output:
                with open(args.exam_output, "w", encoding="utf-8") as f:
                    f.write(result["combined_exam_markdown"])
                print(f"Combined examination paper written to {args.exam_output}")

            print(f"\nGenerated {result['total_questions_generated']} CBSE questions across {len(result['topics'])} topics based on weights.")

        else:
            effective_target = "auto" if is_dynamic else args.target_questions
            manifest = asyncio.run(
                compute_chapter_weights(
                    chunks_endpoint=chunks_src,
                    toc_endpoint=toc_src,
                    questions_endpoint=questions_src,
                    ollama_url=args.ollama_url,
                    concurrency=args.concurrency,
                    target_questions=effective_target,
                    dynamic_target=is_dynamic,
                    min_questions=args.min_questions,
                    max_questions=args.max_questions,
                    mock_mode=args.mock,
                )
            )
            json_output = json.dumps(manifest, indent=2)
            print(json_output)

            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(json_output)
                print(f"Manifest successfully written to {args.output}")

    except Exception as exc:
        print(f"Orchestration pipeline error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
