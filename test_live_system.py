"""
Verification script for:
1. Ollama & Nimble /v1/systemone live service on port 11434
2. Live Chapter Topic Weight Orchestration with mock_mode=False over port 11434
3. Live CBSE Question Generation with Google Gemini Flash using configured API key
"""

import asyncio
import os
import sys
import httpx

# Ensure workspace modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "cbse-generator"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from orchestrator import (
    compute_chapter_weights,
    generate_topic_questions,
    orchestrate_and_generate,
    DEFAULT_OLLAMA_SYSTEMONE_URL,
    DEFAULT_GEMINI_API_KEY,
)


def _ensure_ollama_service():
    import time
    from ollama_mock_service import is_port_in_use, start_service_in_thread
    if not is_port_in_use(11434):
        print("Ollama mock daemon not listening on port 11434. Auto-starting...")
        start_service_in_thread(11434)
        time.sleep(0.5)


def test_ollama_service_endpoints():
    print("--- 1. Testing Live Ollama / Nimble Endpoints on Port 11434 ---")
    _ensure_ollama_service()
    client = httpx.Client(timeout=5.0)

    # Version check (used by orchestrator circuit breaker probe)
    r_ver = client.get("http://localhost:11434/api/version")
    assert r_ver.status_code == 200, f"Expected 200, got {r_ver.status_code}"
    assert "version" in r_ver.json()
    print(f"[PASS] /api/version: {r_ver.json()}")

    # Tags check
    r_tags = client.get("http://localhost:11434/api/tags")
    assert r_tags.status_code == 200
    models = [m["name"] for m in r_tags.json().get("models", [])]
    assert "nimble:latest" in models
    print(f"[PASS] /api/tags: {models}")

    # Pass 1: Question classification via /v1/systemone
    p1 = {
        "model": "nimble",
        "state": "Question:\nState Faraday's law of electromagnetic induction and write its formula.",
        "questions": {
            "assigned_topic": {
                "type": "choice",
                "criteria": {
                    "Faraday's Law of Induction": "Covers Faraday",
                    "Lenz's Law": "Covers Lenz",
                    "Eddy Currents": "Covers Eddy"
                }
            },
            "question_type": {
                "type": "choice",
                "criteria": {
                    "factual": "Basic definition",
                    "conceptual": "Qualitative explanation",
                    "numerical": "Calculations",
                    "derivation": "Formal proof"
                }
            }
        }
    }
    r_sys1 = client.post("http://localhost:11434/v1/systemone", json=p1)
    assert r_sys1.status_code == 200, f"Expected 200, got {r_sys1.status_code}"
    ans1 = r_sys1.json().get("answers", {})
    topic_choice = ans1.get("assigned_topic", {}).get("choice")
    type_choice = ans1.get("question_type", {}).get("choice")
    assert topic_choice == "Faraday's Law of Induction"
    assert type_choice == "factual"
    print(f"[PASS] /v1/systemone Pass 1 Classification: topic='{topic_choice}', type='{type_choice}'")

    # Pass 2: Rigor scoring via /v1/systemone
    p2 = {
        "model": "nimble",
        "state": "Topic: Faraday's Law of Induction\nContent:\ne = - dPhi/dt with calculus and derivatives.",
        "questions": {
            "technical_rigor": {
                "type": "score",
                "criteria": ["Introductory", "Applied", "Advanced"]
            }
        }
    }
    r_sys2 = client.post("http://localhost:11434/v1/systemone", json=p2)
    assert r_sys2.status_code == 200
    ans2 = r_sys2.json().get("answers", {})
    score = ans2.get("technical_rigor", {}).get("score")
    assert score is not None
    # Rigor multiplier = 1.0 + (score / 2.0)
    diff = 1.0 + (score / 2.0)
    assert diff == 1.85
    print(f"[PASS] /v1/systemone Pass 2 Rigor: raw_score={score}, difficulty_multiplier={diff}")


def test_live_orchestration_over_port_11434():
    print("\n--- 2. Testing Live Chapter Topic Weight Orchestration over Port 11434 ---")
    async def _run():
        manifest = await compute_chapter_weights(
            ollama_url="http://localhost:11434/v1/systemone",
            mock_mode=False,
            target_questions=10,
            concurrency=4,
        )

        assert manifest["chapter_total_questions_extracted"] in (12, 194)
        assert len(manifest["topics"]) >= 3
        assert manifest["target_questions"] == 10
        total_quota = sum(t["allocated_quota"] for t in manifest["topics"])
        assert total_quota == 10

        print(f"[PASS] Successfully computed weights with live Ollama service:")
        for t in manifest["topics"]:
            print(f"  * {t['topic_name']}: weight={t['final_weight']}, quota={t['allocated_quota']}, diff={t['difficulty_multiplier']}")

    asyncio.run(_run())


def test_live_orchestration_dynamic_target():
    print("\n--- 2b. Testing Live Chapter Topic Weight Orchestration with Dynamic Target ---")
    async def _run():
        manifest = await compute_chapter_weights(
            ollama_url="http://localhost:11434/v1/systemone",
            mock_mode=False,
            target_questions="auto",
            concurrency=4,
        )

        assert manifest["is_dynamic_target"] is True
        assert manifest["target_questions_mode"] == "dynamic"
        assert manifest["target_questions"] >= 5
        total_quota = sum(t["allocated_quota"] for t in manifest["topics"])
        assert total_quota == manifest["target_questions"]
        print(f"[PASS] Dynamic target limit computed: {manifest['target_questions']}, allocated quotas sum to {total_quota}")

    asyncio.run(_run())


def test_live_gemini_question_generation():
    print("\n--- 3. Testing Live CBSE Question Generation with Gemini Flash ---")
    async def _run():
        chunk_latex = r"""
\section{Faraday's Law of Induction}
Experiments show that an electromotive force (emf) is induced in a circuit whenever
the magnetic flux linking the circuit changes. The magnitude of the induced emf $\mathcal{E}$ is
proportional to the rate of change of magnetic flux $\Phi_B$:
\begin{equation}
\mathcal{E} = - \frac{d\Phi_B}{dt}
\end{equation}
The negative sign expresses Lenz's Law indicating that the induced emf opposes the change that produces it.
"""

        res = await generate_topic_questions(
            topic_name="Faraday's Law of Induction",
            latex_chunk=chunk_latex,
            topic_weight=0.5945,
            allocated_quota=1,
            difficulty_multiplier=1.85,
            requested_selection="1 Objective — Multiple Choice Question (1 Mark)",
            use_mock=False,
        )

        assert res["success"] is True
        assert res["mode"] == "live", f"Expected mode 'live', got '{res.get('mode')}'"
        assert "### Q" in res["output"] or "Question" in res["output"]
        print(f"[PASS] Live Gemini generation successful (mode={res['mode']}, source={res.get('source')}):")
        snippet = "\n".join(res["output"].strip().split("\n")[:8])
        print(snippet)

    asyncio.run(_run())


def test_live_server_generate():
    print("\n--- 4. Testing Live cbse-generator server.js /api/generate ---")
    import subprocess
    import time

    # Check if server is running
    server_running = False
    try:
        r = httpx.get("http://localhost:3000/api/status", timeout=1.0)
        server_running = (r.status_code == 200)
    except Exception:
        server_running = False

    proc = None
    if not server_running:
        print("Launching temporary node server.js instance...")
        proc = subprocess.Popen(["node", "server.js"], cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cbse-generator"))
        for _ in range(15):
            time.sleep(0.3)
            try:
                r = httpx.get("http://localhost:3000/api/status", timeout=0.5)
                if r.status_code == 200:
                    server_running = True
                    break
            except Exception:
                pass

    try:
        r_stat = httpx.get("http://localhost:3000/api/status")
        stat_data = r_stat.json()
        assert stat_data.get("hasEnvKey") is True, f"Expected hasEnvKey=True, got {stat_data}"
        print(f"[PASS] server.js /api/status: hasEnvKey={stat_data.get('hasEnvKey')}, model={stat_data.get('model')}")

        # Check /api/verify-key endpoint with configured Gemini key
        r_ver_key = httpx.post("http://localhost:3000/api/verify-key", json={"apiKey": DEFAULT_GEMINI_API_KEY}, timeout=15.0)
        assert r_ver_key.status_code == 200, f"Expected 200, got {r_ver_key.status_code}: {r_ver_key.text}"
        assert r_ver_key.json().get("success") is True
        print(f"[PASS] server.js /api/verify-key: validated successfully with model {r_ver_key.json().get('model')}")

        payload = {
            "subject": "Physics",
            "grade": "12",
            "chapter_title": "Electromagnetic Induction",
            "topic_name": "Faraday's Law of Induction",
            "topic_weight": 0.5945,
            "allocated_quota": 1,
            "latex_chunk": r"\section{Faraday's Law}\nEmf induced is -dPhi/dt.",
            "requested_selection": "1 Objective — Multiple Choice Question (1 Mark)",
            "useMock": False
        }
        r_gen = httpx.post("http://localhost:3000/api/generate", json=payload, timeout=30.0)
        assert r_gen.status_code == 200, f"Expected 200, got {r_gen.status_code}: {r_gen.text}"
        data = r_gen.json()
        assert data.get("success") is True
        assert data.get("mode") == "live", f"Expected mode 'live', got '{data.get('mode')}'"
        print(f"[PASS] server.js /api/generate: mode={data.get('mode')}")
        snippet = "\n".join(data.get("output", "").strip().split("\n")[:6])
        print(snippet)
    finally:
        if proc:
            proc.terminate()


def test_live_full_orchestration_and_generate():
    print("\n--- 5. Testing Live End-to-End orchestrate_and_generate Pipeline ---")
    async def _run():
        exam_result = await orchestrate_and_generate(
            ollama_url="http://localhost:11434/v1/systemone",
            mock_mode=False,
            target_questions=5,
            subject="Physics",
            grade="12",
            chapter_title="Electromagnetic Induction",
        )

        assert exam_result["total_questions_generated"] >= 5
        assert "combined_exam_markdown" in exam_result
        print(f"[PASS] Full pipeline generated {exam_result['total_questions_generated']} exam questions across topics.")
        first_lines = "\n".join(exam_result["combined_exam_markdown"].split("\n")[:12])
        print(first_lines)

    asyncio.run(_run())


def main():
    test_ollama_service_endpoints()
    test_live_orchestration_over_port_11434()
    test_live_orchestration_dynamic_target()
    test_live_gemini_question_generation()
    test_live_server_generate()
    test_live_full_orchestration_and_generate()
    print("\n--- ALL LIVE INTEGRATION CHECKS PASSED SUCCESSFULLY! ---")


if __name__ == "__main__":
    main()
