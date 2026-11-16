"""
Local Mock Daemon / Service Adapter for Ollama and Nimble /v1/systemone
Runs on port 11434 to provide a high-fidelity local service for Ollama
and the Nimble System 1 decision engine (/v1/systemone).

Serves:
- GET  /                     -> 200 Ollama is running
- GET  /api/version          -> 200 {"version": "0.1.32"}
- GET  /api/tags             -> 200 Model list with nimble
- POST /api/show             -> 200 Model info for nimble
- POST /v1/systemone         -> 200 Nimble System 1 Classification & Rigor Scoring
- POST /api/generate         -> 200 Standard generation mock
- POST /api/chat             -> 200 Standard chat mock
- POST /v1/chat/completions  -> 200 OpenAI-compatible mock
- GET  /health               -> 200 Health check
"""

import argparse
import json
import os
import re
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_PORT = 11434
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".ollama_service.pid")

# ----------------- COGNITIVE FORMATS & HEURISTICS -----------------
def get_canonical_type(raw_type: Any, is_derivation: bool = False, is_numerical: bool = False) -> str:
    if is_derivation:
        return "derivation"
    if is_numerical:
        return "numerical"
    t_clean = str(raw_type or "").strip().lower()
    mapping = {
        "factual": "factual", "recall": "factual", "definition": "factual", "direct": "factual",
        "mcq": "factual", "fill": "factual", "fill-in-the-blank": "factual", "tf": "factual",
        "true-false": "factual", "match": "factual", "objective": "factual",
        "conceptual": "conceptual", "qualitative": "conceptual", "understanding": "conceptual",
        "reasoning": "conceptual", "short": "conceptual", "long": "conceptual", "case": "conceptual",
        "numerical": "numerical", "calculation": "numerical", "computational": "numerical",
        "derivation": "derivation", "proof": "derivation", "derive": "derivation",
    }
    if t_clean in mapping:
        return mapping[t_clean]
    for key in ("derivation", "numerical", "factual", "conceptual"):
        if key in t_clean:
            return key
    return "conceptual"


def classify_question_heuristic(question_text: str, toc_topics: List[str]) -> Tuple[str, str]:
    """Heuristic classifier for question topic and cognitive format."""
    q_str = str(question_text or "")
    q_lower = q_str.lower()

    topic_keywords = {
        "faraday": ["faraday", "flux", "emf", "electromotive", "n turns", "rotating coil", "rate of change"],
        "lenz": ["lenz", "direction of induced", "oppose", "conservation of energy", "polarity", "magnet falls", "approaches"],
        "eddy": ["eddy", "damping", "galvanometer", "dead-beat", "copper plate", "pendulum", "laminat", "swirling", "foucault"],
        "types of solutions": ["amalgam", "camphor", "types of solution", "binary solution", "solute and solvent"],
        "expressing concentration": ["molarity", "molality", "mole fraction", "ppm", "mass percentage"],
        "solubility of a solid": ["solid in liquid", "dissolution", "crystallisation", "saturated solution"],
        "solubility of a gas": ["gas in liquid", "henry", "k_h", "aquatic species", "anoxia", "bends", "scuba"],
        "vapour pressure of liquid-liquid": ["liquid-liquid", "raoult", "volatile liquids", "partial vapour pressure"],
        "ideal solutions": ["ideal solution", "delta_mix h = 0", "delta_mix v = 0", "benzene and toluene"],
        "non-ideal solutions": ["non-ideal", "positive deviation", "negative deviation", "azeotrope", "azeotropic"],
        "relative lowering": ["relative lowering", "p_1^0", "lowering of vapour pressure"],
        "elevation of boiling": ["elevation of boiling", "boiling point elevation", "ebullioscopic", "k_b", "delta t_b"],
        "depression of freezing": ["depression of freezing", "freezing point depression", "cryoscopic", "k_f", "delta t_f"],
        "osmosis and osmotic": ["osmosis", "osmotic pressure", "semipermeable", "semi-permeable", "isotonic", "pi = crt"],
        "abnormal molar": ["abnormal molar", "van 't hoff", "factor i", "degree of dissociation", "dimerisation"],
    }

    best_topic = toc_topics[0] if toc_topics else "General"
    max_score = -1

    for topic in toc_topics:
        score = 0
        topic_lower = topic.lower()

        # Check explicit topic number
        num_m = re.match(r"^(\d+\.\d+(?:\.\d+)*)", topic)
        if num_m:
            t_num = num_m.group(1)
            if re.search(r"\b" + re.escape(t_num) + r"\b", q_lower):
                score += 50
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

    # Determine cognitive format
    is_deriv = any(k in q_lower for k in ("derive", "derivation", "first principles", "proof", "prove"))
    is_num = any(k in q_lower for k in ("calculate", "numerical", "find the value", "magnitude of", "mass of", "mole fraction")) or re.search(
        r"\d+\.?\d*\s*(t/s|cm|m|v|a|wb|ohm|rad|g|kg|mol|bar|torr|kpa|atm|mmhg|pm|nm)", q_lower
    )

    if is_deriv:
        q_type = "derivation"
    elif is_num:
        q_type = "numerical"
    elif any(k in q_lower for k in ("explain", "why", "consequence", "predict", "plot", "reason")):
        q_type = "conceptual"
    elif any(k in q_lower for k in ("state", "define", "what are", "what is", "unit", "formula", "mention", "which of")):
        q_type = "factual"
    else:
        q_type = "conceptual"

    return best_topic, q_type


def score_topic_difficulty_heuristic(topic_name: str, topic_latex_text: str) -> float:
    """Heuristic difficulty scorer for topic content producing difficulty multiplier [1.0, 2.0]."""
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


# ----------------- HTTP REQUEST HANDLER -----------------
class OllamaMockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress routine access logs unless debugging
        if os.getenv("OLLAMA_SERVICE_DEBUG", "0") == "1":
            super().log_message(format, *args)

    def _set_cors_headers(self, status: int = 200, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS, HEAD")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")

    def do_OPTIONS(self) -> None:
        self._set_cors_headers(204, "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self._set_cors_headers(200, "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "" or path == "/":
            body = b"Ollama is running\n"
            self._set_cors_headers(200, "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/version":
            res_obj = {"version": "0.1.32"}
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/tags":
            res_obj = {
                "models": [
                    {
                        "name": "nimble:latest",
                        "model": "nimble:latest",
                        "modified_at": "2026-10-17T07:00:00.0000000Z",
                        "size": 7365960960,
                        "digest": "sha256:nimble7b00000000000000000000000000000000000000000000000000000000",
                        "details": {
                            "parent_model": "",
                            "format": "gguf",
                            "family": "nimble",
                            "families": ["nimble"],
                            "parameter_size": "7B",
                            "quantization_level": "Q4_0",
                        },
                    }
                ]
            }
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/health", "/api/health"):
            res_obj = {"status": "ok", "service": "ollama-nimble-systemone-adapter", "port": DEFAULT_PORT}
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Default 404 for unknown GET
        res_obj = {"error": f"Endpoint {self.path} not found"}
        body = json.dumps(res_obj).encode("utf-8")
        self._set_cors_headers(404, "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_len = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            payload = json.loads(raw_body)
        except Exception:
            payload = {}

        # ----------------- /v1/systemone (NIMBLE SYSTEM 1 DECISION ENGINE) -----------------
        if path == "/v1/systemone":
            state = payload.get("state", "")
            questions = payload.get("questions", {})
            model = payload.get("model", "nimble")
            answers = {}

            # Pass 1: Question Topic & Type Classification
            if "assigned_topic" in questions or "question_type" in questions:
                q_text = state
                if q_text.startswith("Question:\n"):
                    q_text = q_text[len("Question:\n"):].strip()

                topic_info = questions.get("assigned_topic", {})
                criteria = topic_info.get("criteria", {})
                toc_topics = list(criteria.keys()) if isinstance(criteria, dict) else []

                best_topic, q_type = classify_question_heuristic(q_text, toc_topics)

                if "assigned_topic" in questions:
                    answers["assigned_topic"] = {"choice": best_topic}
                if "question_type" in questions:
                    answers["question_type"] = {"choice": q_type}

            # Pass 2: Topic Technical Rigor Scoring
            if "technical_rigor" in questions:
                # Format: "Topic: {topic_name}\nContent:\n{snippet}"
                topic_name = ""
                content_text = ""
                if "Topic:" in state:
                    m = re.search(r"Topic:\s*(.*?)(?:\nContent:\n|\n\n|\Z)", state, re.DOTALL)
                    if m:
                        topic_name = m.group(1).strip()
                if "Content:\n" in state:
                    content_text = state.split("Content:\n", 1)[1]
                else:
                    content_text = state

                diff = score_topic_difficulty_heuristic(topic_name, content_text)
                # Map diff multiplier [1.0, 2.0] back to 3-tier raw rubric score [0.0, 2.0]
                raw_score = round(max(0.0, min(2.0, (diff - 1.0) * 2.0)), 4)
                answers["technical_rigor"] = {"score": raw_score}

            # If generic payload without recognized questions
            if not answers:
                for q_key, q_val in questions.items():
                    if isinstance(q_val, dict) and q_val.get("type") == "choice":
                        crit = list(q_val.get("criteria", {}).keys())
                        choice = crit[0] if crit else "default"
                        answers[q_key] = {"choice": choice}
                    elif isinstance(q_val, dict) and q_val.get("type") == "score":
                        answers[q_key] = {"score": 1.0}
                    else:
                        answers[q_key] = {"choice": "default"}

            res_obj = {
                "model": model,
                "answers": answers,
            }
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ----------------- /api/show (OLLAMA MODEL INFO) -----------------
        if path == "/api/show":
            res_obj = {
                "license": "Apache 2.0",
                "modelfile": "FROM nimble:latest",
                "parameters": "stop \"<|end|>\"",
                "template": "{{ .Prompt }}",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "nimble",
                    "families": ["nimble"],
                    "parameter_size": "7B",
                    "quantization_level": "Q4_0",
                },
            }
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ----------------- /api/generate & /api/chat -----------------
        if path == "/api/generate":
            res_obj = {
                "model": payload.get("model", "nimble"),
                "created_at": "2026-10-17T07:00:00Z",
                "response": "Nimble response generated successfully.",
                "done": True,
            }
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/chat":
            res_obj = {
                "model": payload.get("model", "nimble"),
                "created_at": "2026-10-17T07:00:00Z",
                "message": {"role": "assistant", "content": "Nimble assistant response."},
                "done": True,
            }
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/v1/chat/completions":
            res_obj = {
                "id": "chatcmpl-nimble-mock",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": payload.get("model", "nimble"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Nimble response."},
                        "finish_reason": "stop",
                    }
                ],
            }
            body = json.dumps(res_obj).encode("utf-8")
            self._set_cors_headers(200, "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Unknown POST
        res_obj = {"error": f"Endpoint {self.path} not found"}
        body = json.dumps(res_obj).encode("utf-8")
        self._set_cors_headers(404, "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ----------------- SERVER LIFECYCLE -----------------
def is_port_in_use(port: int = DEFAULT_PORT, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def create_server(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), OllamaMockHandler)
    server.daemon_threads = True
    return server


def start_service_in_thread(port: int = DEFAULT_PORT, host: str = "0.0.0.0") -> Tuple[ThreadingHTTPServer, threading.Thread]:
    """Starts the Ollama mock service in a background daemon thread."""
    server = create_server(host=host, port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def run_foreground(host: str = "0.0.0.0", port: int = DEFAULT_PORT) -> None:
    """Runs the service in the foreground until interrupted."""
    print(f"Starting Ollama & Nimble /v1/systemone Service Adapter on http://{host}:{port}...")
    server = create_server(host=host, port=port)
    # Save PID
    try:
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    try:
        print(f"Ollama mock daemon running. Probing endpoints:")
        print(f" - Version check: http://localhost:{port}/api/version")
        print(f" - Systemone API: http://localhost:{port}/v1/systemone")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Ollama service...")
    finally:
        server.shutdown()
        server.server_close()
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except Exception:
                pass


def stop_service_by_pid() -> bool:
    """Stops the daemon using PID_FILE if present."""
    if not os.path.exists(PID_FILE):
        return False
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        time.sleep(0.5)
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"Failed to stop service: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Ollama & Nimble /v1/systemone Service Adapter")
    parser.add_argument("action", nargs="?", default="run", choices=["run", "start", "stop", "status"])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")
    args = parser.parse_args()

    if args.action == "status":
        in_use = is_port_in_use(args.port)
        print(f"Port {args.port} status: {'ACTIVE (Listening)' if in_use else 'INACTIVE (Closed)'}")
        sys.exit(0 if in_use else 1)

    elif args.action == "stop":
        stopped = stop_service_by_pid()
        print(f"Service stop request: {'Success' if stopped else 'No running daemon found via PID file'}")

    elif args.action in ("run", "start"):
        if is_port_in_use(args.port):
            print(f"Notice: Port {args.port} is already in use by a running process.")
            sys.exit(0)
        run_foreground(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
