import asyncio
import json
import os
import re
import pytest
import httpx
from orchestrator import (
    align_chunks_to_topics,
    build_requested_selection,
    chunk_latex_document,
    classify_question_with_nimble,
    compute_chapter_weights,
    extract_latex_headers,
    fetch_prompt_template,
    generate_chapter_questions,
    generate_topic_questions,
    get_canonical_type,
    get_type_weight,
    mock_classify_question,
    mock_generate_cbse_questions,
    mock_score_topic_difficulty,
    normalize_chunks,
    normalize_question_records,
    normalize_questions,
    normalize_toc_topics,
    orchestrate_and_generate,
    render_cbse_user_prompt,
    score_topic_difficulty_with_nimble,
    select_candidate_topics,
    synthesize_topic_weights,
    compute_dynamic_target_questions,
    apportion_quotas,
    parse_args,
    CBSE_SYSTEM_PROMPT,
    CBSE_USER_PROMPT_TEMPLATE,
    TYPE_WEIGHTS,
    _INGESTION_CACHE,
    clear_ingestion_cache,
    _fetch_with_fallback,
    MIN_MANUAL_QUESTIONS,
    MAX_MANUAL_QUESTIONS,
    validate_target_questions,
)


def test_synthesize_topic_weights_formula():
    """Verify deterministic aggregation formula: W_j = (I_j + 0.5) * D_j and P_j = W_j / sum(W_k)."""
    toc_topics = ["Faraday's Law of Induction", "Lenz's Law", "Eddy Currents"]

    # Question results simulating 4 Faraday, 5 Lenz, 3 Eddy
    # Faraday: 1 conceptual (1.5), 1 numerical (2.0), 2 derivations (2 * 2.5 = 5.0) -> sum = 8.5
    # Lenz: 1 factual (1.0), 4 conceptual (4 * 1.5 = 6.0) -> sum = 7.0
    # Eddy: 3 conceptual (3 * 1.5 = 4.5) -> sum = 4.5
    question_results = [
        {"topic": "Faraday's Law of Induction", "type": "conceptual"},
        {"topic": "Faraday's Law of Induction", "type": "numerical"},
        {"topic": "Faraday's Law of Induction", "type": "derivation"},
        {"topic": "Faraday's Law of Induction", "type": "derivation"},
        {"topic": "Lenz's Law", "type": "factual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Eddy Currents", "type": "conceptual"},
        {"topic": "Eddy Currents", "type": "conceptual"},
        {"topic": "Eddy Currents", "type": "conceptual"},
    ]

    difficulty_results = {
        "Faraday's Law of Induction": 1.85,
        "Lenz's Law": 1.25,
        "Eddy Currents": 1.40,
    }

    manifest = synthesize_topic_weights(
        toc_topics=toc_topics,
        question_results=question_results,
        difficulty_results=difficulty_results,
        prior=0.5,
    )

    assert manifest["chapter_total_questions_extracted"] == 12
    assert len(manifest["topics"]) == 3

    topics_dict = {t["topic_name"]: t for t in manifest["topics"]}

    # Faraday
    assert topics_dict["Faraday's Law of Induction"]["existing_questions"] == 4
    assert topics_dict["Faraday's Law of Induction"]["question_impact_score"] == 8.5
    assert topics_dict["Faraday's Law of Induction"]["difficulty_multiplier"] == 1.85
    # W1 = (8.5 + 0.5) * 1.85 = 16.65
    # W2 = (7.0 + 0.5) * 1.25 = 9.375
    # W3 = (4.5 + 0.5) * 1.40 = 7.00
    # Total = 33.025
    expected_w1 = round(16.65 / 33.025, 4)
    expected_w2 = round(9.375 / 33.025, 4)
    expected_w3 = round(7.00 / 33.025, 4)

    assert topics_dict["Faraday's Law of Induction"]["final_weight"] == expected_w1
    assert topics_dict["Lenz's Law"]["final_weight"] == expected_w2
    assert topics_dict["Eddy Currents"]["final_weight"] == expected_w3

    # Weights sum to ~1.0
    total_weights = sum(t["final_weight"] for t in manifest["topics"])
    assert 0.999 <= total_weights <= 1.001


def test_downstream_allocation_quotas():
    """Verify allocation quotas sum exactly to target_questions N using Hamilton method."""
    toc_topics = ["Topic A", "Topic B", "Topic C"]
    question_results = [
        {"topic": "Topic A", "type": "derivation"},  # 2.5
        {"topic": "Topic A", "type": "numerical"},   # 2.0
        {"topic": "Topic B", "type": "factual"},     # 1.0
        {"topic": "Topic C", "type": "factual"},     # 1.0
    ]
    difficulty_results = {"Topic A": 1.8, "Topic B": 1.2, "Topic C": 1.0}

    manifest = synthesize_topic_weights(
        toc_topics=toc_topics,
        question_results=question_results,
        difficulty_results=difficulty_results,
        target_questions=20,
    )

    assert manifest["target_questions"] == 20
    quotas = [t["allocated_quota"] for t in manifest["topics"]]
    assert sum(quotas) == 20
    # Topic A should receive the highest quota
    assert quotas[0] > quotas[1] >= quotas[2]


def test_align_chunks_to_topics():
    """Verify alignment of LaTeX chunks to TOC topics via metadata, LaTeX headers, and fallbacks."""
    toc_topics = ["Refraction through a Prism", "Minimum Deviation", "Nernst Equation"]

    chunks = [
        # Explicit topic field
        {"topic": "Refraction through a Prism", "text": "Light enters prism face AB."},
        # LaTeX subsection header
        {"text": "\\subsection{Minimum Deviation}\nAngle of minimum deviation is Dm."},
        # Header in text content fallback
        {"text": "Here we analyze the Nernst Equation for concentration dependence."},
    ]

    aligned = align_chunks_to_topics(chunks, toc_topics)
    assert "Light enters prism" in aligned["Refraction through a Prism"]
    assert "Minimum Deviation" in aligned["Minimum Deviation"]
    assert "Nernst Equation" in aligned["Nernst Equation"]


def test_extract_latex_headers():
    tex = r"\section{Main Theory}\nText\n\subsection*{Special Case}\nMore text\n\chapter{Intro}"
    headers = extract_latex_headers(tex)
    assert headers == ["Main Theory", "Special Case", "Intro"]


def test_extract_latex_headers_nested_commands_and_subsubsections():
    r"""Verify that balanced brace matching extracts headers with \textbf{}, \label{}, \subsubsection."""
    tex = (
        r"\section{\label{sec:emi}Faraday's Law of \textbf{Induction}}"
        r"\subsubsection{Eddy Currents in Transformers}"
        r"\paragraph{Summary Notes}"
    )
    headers = extract_latex_headers(tex)
    assert headers == [
        "Faraday's Law of Induction",
        "Eddy Currents in Transformers",
        "Summary Notes",
    ]


def test_normalizers():
    # normalize_chunks
    assert len(normalize_chunks([{"text": "a"}])) == 1
    assert len(normalize_chunks({"chunks": [{"text": "b"}]})) == 1
    assert len(normalize_chunks({"topics": [{"content": "c"}]})) == 1

    # normalize_toc_topics
    assert normalize_toc_topics(["T1", "T2"]) == ["T1", "T2"]
    assert normalize_toc_topics({"topics": ["T1", "T2"]}) == ["T1", "T2"]
    assert normalize_toc_topics({"topics": [{"title": "T1"}, {"title": "T2"}]}) == ["T1", "T2"]

    # normalize_questions
    assert normalize_questions(["Q1", "Q2"]) == ["Q1", "Q2"]
    assert normalize_questions({"questions": ["Q1", "Q2"]}) == ["Q1", "Q2"]
    assert normalize_questions([{"text": "Q1"}, {"latex": "Q2"}]) == ["Q1", "Q2"]


def test_normalize_nested_toc_schema():
    """Verify normalization of nested chapter JSON where toc is a dictionary containing topics."""
    nested = {
        "chapter": "Electromagnetic Induction",
        "toc": {
            "topics": ["Faraday's Law", "Lenz's Law", "Eddy Currents"]
        }
    }
    topics = normalize_toc_topics(nested)
    assert topics == ["Faraday's Law", "Lenz's Law", "Eddy Currents"]


def test_normalize_chunks_with_plain_strings():
    """Verify that chunks passed as a list of raw LaTeX strings are safely converted to dicts."""
    raw_strings = [
        r"\section{Topic 1} Content 1",
        r"\section{Topic 2} Content 2",
    ]
    chunks = normalize_chunks(raw_strings)
    assert len(chunks) == 2
    assert chunks[0]["text"] == raw_strings[0]
    assert chunks[1]["text"] == raw_strings[1]

    # Verify align_chunks_to_topics handles raw strings without AttributeError
    aligned = align_chunks_to_topics(raw_strings, ["Topic 1", "Topic 2"])
    assert "Content 1" in aligned["Topic 1"]
    assert "Content 2" in aligned["Topic 2"]


def test_synthesize_topic_weights_case_and_whitespace_insensitivity():
    """Verify question results with lowercase/untrimmed topic names map correctly to canonical topics."""
    toc_topics = ["Introductory Concepts", "Faraday's Law of Induction"]
    question_results = [
        {"topic": "faraday's law of induction  ", "type": "numerical"},
    ]
    diff = {"Faraday's Law of Induction": 1.5, "Introductory Concepts": 1.0}

    manifest = synthesize_topic_weights(toc_topics, question_results, diff)
    topics = {t["topic_name"]: t for t in manifest["topics"]}

    assert topics["Faraday's Law of Induction"]["existing_questions"] == 1
    assert topics["Faraday's Law of Induction"]["question_impact_score"] == 2.0
    assert topics["Introductory Concepts"]["existing_questions"] == 0
    assert topics["Introductory Concepts"]["question_impact_score"] == 0.0


def test_synthesize_topic_weights_type_normalization_and_weights():
    """Verify that capitalized or synonym types (e.g. Derivation, Recall, Calculation) get correct weights."""
    toc_topics = ["Topic A"]
    question_results = [
        {"topic": "Topic A", "type": "Derivation"},  # 2.5
        {"topic": "Topic A", "type": "Numerical"},   # 2.0
        {"topic": "Topic A", "type": "Recall"},      # 1.0 (factual)
        {"topic": "Topic A", "type": "Conceptual"},  # 1.5
    ]
    manifest = synthesize_topic_weights(toc_topics, question_results, {"Topic A": 1.0})
    # Total impact = 2.5 + 2.0 + 1.0 + 1.5 = 7.0
    assert manifest["topics"][0]["question_impact_score"] == 7.0


def test_hamilton_apportionment_large_target_exact_sum():
    """Verify that Hamilton apportionment sums exactly to target_questions even for large N."""
    toc_topics = ["Topic 1", "Topic 2", "Topic 3"]
    # 0.28575, 0.28575, 0.4285
    question_results = [
        {"topic": "Topic 1", "type": "numerical"},
        {"topic": "Topic 2", "type": "numerical"},
        {"topic": "Topic 3", "type": "derivation"},
    ]
    diff = {"Topic 1": 1.0, "Topic 2": 1.0, "Topic 3": 1.0}

    manifest = synthesize_topic_weights(toc_topics, question_results, diff, target_questions=100000)
    quotas = [t["allocated_quota"] for t in manifest["topics"]]
    assert sum(quotas) == 100000


def test_target_questions_zero():
    """Verify that target_questions=0 allocates 0 quotas without errors."""
    toc_topics = ["T1", "T2"]
    manifest = synthesize_topic_weights(toc_topics, [], {}, target_questions=0)
    assert manifest["target_questions"] == 0
    assert all(t["allocated_quota"] == 0 for t in manifest["topics"])


def test_candidate_topics_selection_for_over_26_topics():
    """Verify that candidate topic selector prioritizes relevant topics beyond index 25."""
    many_topics = [f"Unrelated Topic {i}" for i in range(35)]
    many_topics[30] = "Electromagnetic Damping in Galvanometers"

    question = "Explain how electromagnetic damping occurs in a galvanometer."
    selected = select_candidate_topics(question, many_topics, max_options=26)

    assert len(selected) == 26
    # Crucial: topic 30 must be included in the candidate list!
    assert "Electromagnetic Damping in Galvanometers" in selected


def test_empty_questions_preserves_prior_coverage():
    """When zero questions are extracted, prior 0.5 guarantees each topic receives a non-zero weight."""
    toc_topics = ["Topic 1", "Topic 2"]
    manifest = synthesize_topic_weights(
        toc_topics=toc_topics,
        question_results=[],
        difficulty_results={"Topic 1": 1.5, "Topic 2": 1.0},
    )
    assert manifest["chapter_total_questions_extracted"] == 0
    assert manifest["topics"][0]["final_weight"] > 0
    assert manifest["topics"][1]["final_weight"] > 0
    # Topic 1 has higher difficulty, so it gets higher weight
    assert manifest["topics"][0]["final_weight"] > manifest["topics"][1]["final_weight"]


def test_empty_toc_topics():
    manifest = synthesize_topic_weights(
        toc_topics=[],
        question_results=[{"topic": "A", "type": "factual"}],
        difficulty_results={},
    )
    assert manifest["topics"] == []
    assert manifest["chapter_total_questions_extracted"] == 1


def test_nimble_choice_options_truncated_to_26():
    """Ensure candidate topic list for Nimble choice question never exceeds 26 items."""
    many_topics = [f"Topic {i}" for i in range(30)]
    recorded_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        recorded_requests.append(data)
        criteria = data["questions"]["assigned_topic"]["criteria"]
        assert len(criteria) <= 26
        return httpx.Response(
            200,
            json={
                "answers": {
                    "assigned_topic": {"choice": "Topic 0"},
                    "question_type": {"choice": "conceptual"},
                }
            },
        )

    async def _run():
        transport = httpx.MockTransport(mock_handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await classify_question_with_nimble(
                client=client,
                question_latex="What is Topic 0?",
                toc_topics=many_topics,
                nimble_url="http://mock-ollama/v1/systemone",
            )
            assert res["topic"] == "Topic 0"
            assert res["type"] == "conceptual"
            assert len(recorded_requests) == 1

    asyncio.run(_run())


def test_nimble_snippet_context_window_limit():
    """Verify snippet length passed to Nimble is capped at 3500 characters."""
    recorded_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content)
        recorded_requests.append(data)
        state = data["state"]
        content_part = state.split("Content:\n")[1]
        assert len(content_part) <= 3500
        return httpx.Response(
            200,
            json={
                "answers": {
                    "technical_rigor": {"score": 1.5}
                }
            },
        )

    async def _run():
        transport = httpx.MockTransport(mock_handler)
        long_text = "x" * 10000  # 10,000 characters
        async with httpx.AsyncClient(transport=transport) as client:
            diff = await score_topic_difficulty_with_nimble(
                client=client,
                topic_name="Long Topic",
                topic_latex_text=long_text,
                nimble_url="http://mock-ollama/v1/systemone",
            )
            assert diff == 1.0 + (1.5 / 2.0)  # 1.75
            assert len(recorded_requests) == 1

    asyncio.run(_run())


def test_end_to_end_mock_transport_pipeline():
    """Tests the full end-to-end orchestration pipeline with mocked endpoints and Ollama /v1/systemone."""
    def router(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "http://test-server/api/chunks":
            return httpx.Response(200, json=[
                {"topic": "Topic A", "text": r"\section{Topic A} Text A with \frac{d\Phi}{dt}"},
                {"topic": "Topic B", "text": r"\section{Topic B} Qualitative principles."},
            ])
        elif url == "http://test-server/api/toc":
            return httpx.Response(200, json={"topics": ["Topic A", "Topic B"]})
        elif url == "http://test-server/api/questions":
            return httpx.Response(200, json={"questions": [
                "Derive formula for Topic A",
                "State law for Topic B",
            ]})
        elif url == "http://test-server/v1/systemone":
            payload = json.loads(request.content)
            if "assigned_topic" in payload["questions"]:
                q_text = payload["state"]
                topic = "Topic A" if "Topic A" in q_text else "Topic B"
                q_type = "derivation" if "Derive" in q_text else "factual"
                return httpx.Response(200, json={
                    "answers": {
                        "assigned_topic": {"choice": topic},
                        "question_type": {"choice": q_type},
                    }
                })
            else:
                topic_state = payload["state"]
                score = 1.6 if "Topic A" in topic_state else 0.4
                return httpx.Response(200, json={
                    "answers": {
                        "technical_rigor": {"score": score}
                    }
                })
        return httpx.Response(404)

    async def _run():
        transport = httpx.MockTransport(router)
        async with httpx.AsyncClient(transport=transport) as client:
            manifest = await compute_chapter_weights(
                chunks_endpoint="http://test-server/api/chunks",
                toc_endpoint="http://test-server/api/toc",
                questions_endpoint="http://test-server/api/questions",
                ollama_url="http://test-server/v1/systemone",
                concurrency=2,
                target_questions=10,
                client=client,
            )

            assert manifest["chapter_total_questions_extracted"] == 2
            assert len(manifest["topics"]) == 2
            assert manifest["target_questions"] == 10
            total_quota = sum(t["allocated_quota"] for t in manifest["topics"])
            assert total_quota == 10

    asyncio.run(_run())


def test_offline_mock_mode():
    """Verify that mock_mode executes completely offline with deterministic heuristics."""
    toc = ["Faraday's Law of Induction", "Lenz's Law", "Eddy Currents"]
    chunks = [
        {"topic": "Faraday's Law of Induction", "text": r"\section{Faraday} \mathcal{E} = -\frac{d\Phi}{dt}"},
        {"topic": "Lenz's Law", "text": r"\section{Lenz} Conservation of energy."},
        {"topic": "Eddy Currents", "text": r"\section{Eddy} Damping and resistance."},
    ]
    questions = [
        "Derive the EMF induced in a rotating coil.",
        "Calculate the induced electromotive force of 5 V.",
        "State Lenz's law and explain conservation of energy.",
        "Explain electromagnetic damping in galvanometers.",
    ]

    def mock_endpoints(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/chunks"):
            return httpx.Response(200, json=chunks)
        elif url.endswith("/api/toc"):
            return httpx.Response(200, json={"topics": toc})
        elif url.endswith("/api/questions"):
            return httpx.Response(200, json={"questions": questions})
        return httpx.Response(404)

    async def _run():
        transport = httpx.MockTransport(mock_endpoints)
        async with httpx.AsyncClient(transport=transport) as client:
            manifest = await compute_chapter_weights(
                chunks_endpoint="http://local/api/chunks",
                toc_endpoint="http://local/api/toc",
                questions_endpoint="http://local/api/questions",
                ollama_url="http://unreachable-ollama:11434/v1/systemone",
                mock_mode=True,
                target_questions=20,
                client=client,
            )

            assert manifest["chapter_total_questions_extracted"] == 4
            assert len(manifest["topics"]) == 3
            assert sum(t["allocated_quota"] for t in manifest["topics"]) == 20

    asyncio.run(_run())


# ----------------- TESTS FOR CBSE PROMPT TEMPLATE & QUESTION GENERATION -----------------


def test_cbse_prompt_template_contract():
    """Verify that CBSE_SYSTEM_PROMPT adheres strictly to the CBSE question generator specification."""
    assert "CBSE QUESTION GENERATOR" in CBSE_SYSTEM_PROMPT
    assert "You are an expert CBSE Senior Secondary" in CBSE_SYSTEM_PROMPT
    assert "{subject}" in CBSE_SYSTEM_PROMPT
    assert "{grade}" in CBSE_SYSTEM_PROMPT
    assert "{chapter_title}" in CBSE_SYSTEM_PROMPT
    assert "{topic_name}" in CBSE_SYSTEM_PROMPT
    assert "{topic_weight}" in CBSE_SYSTEM_PROMPT
    assert "{allocated_quota}" in CBSE_SYSTEM_PROMPT
    assert "{latex_chunk}" in CBSE_SYSTEM_PROMPT
    assert "{requested_selection}" in CBSE_SYSTEM_PROMPT
    assert "STRICT SELECTION & FILTERING RULE" in CBSE_SYSTEM_PROMPT
    assert "CBSE QUESTION TAXONOMY & BLUEPRINT" in CBSE_SYSTEM_PROMPT
    assert "Objective — MCQ" in CBSE_SYSTEM_PROMPT
    assert "Assertion-Reason" in CBSE_SYSTEM_PROMPT
    assert "Very Short Answer" in CBSE_SYSTEM_PROMPT
    assert "Short Answer" in CBSE_SYSTEM_PROMPT
    assert "Case-Based / Source-Based" in CBSE_SYSTEM_PROMPT
    assert "Long Answer (LA) / Derivation" in CBSE_SYSTEM_PROMPT
    assert "### Q[N] | [Category Name] | [Marks] Mark(s) | [🔴 Tier 1 / 🟡 Tier 2]" in CBSE_SYSTEM_PROMPT


def test_render_cbse_user_prompt():
    """Verify rendering of user prompt injects parameters, weights, quotas, and latex chunk."""
    prompt = render_cbse_user_prompt(
        subject="Physics",
        grade="12",
        chapter_title="Electromagnetic Induction",
        topic_name="Faraday's Law of Induction",
        topic_weight=0.5945,
        allocated_quota=6,
        latex_chunk=r"\mathcal{E} = -N\frac{d\Phi_B}{dt}",
        requested_selection="2 MCQs, 1 Assertion-Reason, 1 Short Answer (3M), 2 Long Answer (5M)",
    )

    assert "- {subject}: Physics" in prompt
    assert "- {grade}: 12" in prompt
    assert "- {chapter_title}: Electromagnetic Induction" in prompt
    assert "- {topic_name}: Faraday's Law of Induction" in prompt
    assert "- {topic_weight}: 0.5945" in prompt
    assert "- {allocated_quota}: 6" in prompt
    assert "- {requested_selection}: 2 MCQs, 1 Assertion-Reason, 1 Short Answer (3M), 2 Long Answer (5M)" in prompt
    assert r"\mathcal{E} = -N\frac{d\Phi_B}{dt}" in prompt


def test_build_requested_selection_rigor_tiers():
    """Verify build_requested_selection selects higher cognitive types for high rigor topics."""
    # Quota 1
    sel_high = build_requested_selection(allocated_quota=1, difficulty_multiplier=1.85)
    assert "Short Answer (3M)" in sel_high

    sel_low = build_requested_selection(allocated_quota=1, difficulty_multiplier=1.20)
    assert "MCQ" in sel_low

    # Quota 2
    sel_high2 = build_requested_selection(allocated_quota=2, difficulty_multiplier=1.85)
    assert "Short Answer" in sel_high2 and "Derivation" in sel_high2

    sel_low2 = build_requested_selection(allocated_quota=2, difficulty_multiplier=1.20)
    assert "MCQ" in sel_low2 and "Very Short Answer" in sel_low2

    # Quota 3
    sel_high3 = build_requested_selection(allocated_quota=3, difficulty_multiplier=1.85)
    assert "Derivation" in sel_high3

    # Quota 4
    sel_high4 = build_requested_selection(allocated_quota=4, difficulty_multiplier=1.85)
    assert "Case-Based" in sel_high4 and "Derivation" in sel_high4

    # Quota 6
    sel_high6 = build_requested_selection(allocated_quota=6, difficulty_multiplier=1.85)
    assert "MCQ" in sel_high6 and "Assertion-Reason" in sel_high6 and "Short Answer" in sel_high6

    # Zero or negative quota
    assert build_requested_selection(allocated_quota=0) == "0 questions"

    # Explicit preferred types
    custom = build_requested_selection(
        allocated_quota=4,
        preferred_types=["MCQ", "Numerical (3M)"]
    )
    assert "2 MCQ" in custom and "2 Numerical (3M)" in custom


def test_mock_generate_cbse_questions_format():
    """Verify that mock generator outputs questions matching the CBSE template format."""
    chunk = r"\section{Faraday's Law} \mathcal{E} = -\frac{d\Phi}{dt}"
    output = mock_generate_cbse_questions(
        topic_name="Faraday's Law of Induction",
        latex_chunk=chunk,
        requested_selection="1 MCQ, 1 Short Answer (3M), 1 Long Answer / Derivation (5M)",
        subject="Physics",
        grade="12",
        chapter_title="Electromagnetic Induction",
        start_q_index=1,
    )

    # Check question delimiters
    assert "### Q1 | Objective — MCQ | 1 Mark(s)" in output
    assert "### Q2 | Short Answer (SA) | 3 Mark(s)" in output
    assert "### Q3 | Long Answer (LA) / Derivation | 5 Mark(s)" in output

    # Check mandatory metadata keys
    assert "- **Target Concept:**" in output
    assert "- **Question:**" in output
    assert "- **Marking Scheme & Solution:**" in output
    assert "- **Why this Tier:**" in output
    assert "---" in output


def test_fetch_prompt_template_fallback():
    """Verify fetch_prompt_template returns embedded template when remote server is unreachable."""
    async def _run():
        res = await fetch_prompt_template(base_url="http://unreachable-host:9999")
        assert res["source"] == "embedded_fallback"
        assert res["system_prompt"] == CBSE_SYSTEM_PROMPT
        assert res["user_prompt_template"] == CBSE_USER_PROMPT_TEMPLATE

    asyncio.run(_run())


def test_fetch_prompt_template_from_mock_server():
    """Verify fetch_prompt_template retrieves prompt from server when available."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "success": True,
            "systemPrompt": CBSE_SYSTEM_PROMPT,
            "userPromptTemplate": CBSE_USER_PROMPT_TEMPLATE,
        })

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            res = await fetch_prompt_template(base_url="http://mock-server:3000", client=client)
            assert res["source"] == "remote_api"
            assert res["system_prompt"] == CBSE_SYSTEM_PROMPT

    asyncio.run(_run())


def test_generate_topic_questions_mock_mode():
    """Verify generate_topic_questions offline mock generation."""
    async def _run():
        res = await generate_topic_questions(
            topic_name="Faraday's Law of Induction",
            latex_chunk=r"\mathcal{E} = -N\frac{d\Phi}{dt}",
            topic_weight=0.5945,
            allocated_quota=3,
            difficulty_multiplier=1.85,
            use_mock=True,
            start_q_index=1,
        )

        assert res["success"] is True
        assert res["mode"] == "mock"
        assert res["topic_weight"] == 0.5945
        assert res["allocated_quota"] == 3
        assert "### Q1" in res["output"]
        assert "prompt" in res
        assert res["prompt"]["system_prompt"] == CBSE_SYSTEM_PROMPT
        assert "- {topic_weight}: 0.5945" in res["prompt"]["user_prompt"]

    asyncio.run(_run())


def test_generate_chapter_questions_multi_topic():
    """Verify generate_chapter_questions produces sequential numbering across all manifest topics."""
    manifest = {
        "chapter_total_questions_extracted": 12,
        "target_questions": 5,
        "topics": [
            {
                "topic_name": "Faraday's Law of Induction",
                "final_weight": 0.60,
                "allocated_quota": 3,
                "difficulty_multiplier": 1.85,
            },
            {
                "topic_name": "Lenz's Law",
                "final_weight": 0.20,
                "allocated_quota": 1,
                "difficulty_multiplier": 1.25,
            },
            {
                "topic_name": "Eddy Currents",
                "final_weight": 0.20,
                "allocated_quota": 1,
                "difficulty_multiplier": 1.40,
            },
        ],
    }

    chunks = [
        {"topic": "Faraday's Law of Induction", "text": "Faraday induced emf"},
        {"topic": "Lenz's Law", "text": "Lenz direction of current"},
        {"topic": "Eddy Currents", "text": "Eddy damping and transformers"},
    ]

    async def _run():
        result = await generate_chapter_questions(
            manifest=manifest,
            chunks=chunks,
            subject="Physics",
            grade="12",
            chapter_title="Electromagnetic Induction",
            use_mock=True,
        )

        assert result["subject"] == "Physics"
        assert len(result["topics"]) == 3
        exam_md = result["combined_exam_markdown"]

        # Questions should be sequentially numbered across topics
        assert "### Q1" in exam_md
        assert "### Q2" in exam_md
        assert "### Q3" in exam_md
        assert "### Q4" in exam_md
        assert "### Q5" in exam_md

        # Section headers should record weights and quotas
        assert "Topic Section: Faraday's Law of Induction (Syllabus Weight: 60.0% | Allocated Quota: 3)" in exam_md
        assert "Topic Section: Lenz's Law" in exam_md
        assert "Topic Section: Eddy Currents" in exam_md

    asyncio.run(_run())


def test_orchestrate_and_generate_end_to_end_mock():
    """Verify the full end-to-end pipeline: chunks -> Nimble passes -> weights -> prompt template -> generated exam."""
    toc = ["Faraday's Law of Induction", "Lenz's Law", "Eddy Currents"]
    chunks = [
        {"topic": "Faraday's Law of Induction", "text": r"\section{Faraday} \mathcal{E} = -\frac{d\Phi}{dt}"},
        {"topic": "Lenz's Law", "text": r"\section{Lenz} Conservation of energy."},
        {"topic": "Eddy Currents", "text": r"\section{Eddy} Damping in galvanometers."},
    ]
    questions = [
        "Derive the EMF induced in a rotating coil.",
        "Calculate the induced electromotive force of 5 V.",
        "State Lenz's law and explain conservation of energy.",
        "Explain electromagnetic damping in galvanometers.",
    ]

    def mock_server(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/chunks"):
            return httpx.Response(200, json=chunks)
        elif url.endswith("/api/toc"):
            return httpx.Response(200, json={"topics": toc})
        elif url.endswith("/api/questions"):
            return httpx.Response(200, json={"questions": questions})
        return httpx.Response(404)

    async def _run():
        transport = httpx.MockTransport(mock_server)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await orchestrate_and_generate(
                chunks_endpoint="http://mock-api/api/chunks",
                toc_endpoint="http://mock-api/api/toc",
                questions_endpoint="http://mock-api/api/questions",
                ollama_url="http://mock-api/v1/systemone",
                target_questions=10,
                mock_mode=True,
                client=client,
            )

            manifest = result["manifest"]
            assert manifest["chapter_total_questions_extracted"] == 4
            assert len(manifest["topics"]) == 3

            # Total quota allocated across topics equals target_questions
            total_quota = sum(t["allocated_quota"] for t in manifest["topics"])
            assert total_quota == 10

            # Topic 1 (Faraday) has highest difficulty and existing questions -> receives largest quota
            t_dict = {t["topic_name"]: t for t in manifest["topics"]}
            assert t_dict["Faraday's Law of Induction"]["allocated_quota"] > t_dict["Lenz's Law"]["allocated_quota"]

            # Exam output is populated
            assert len(result["topics"]) == 3
            assert "CENTRAL BOARD OF SECONDARY EDUCATION" in result["combined_exam_markdown"]
            assert "Faraday's Law of Induction" in result["combined_exam_markdown"]
            assert result["total_questions_generated"] == 10
            assert result.get("prompt_template_source") in ("remote_api", "embedded_fallback")

            # Sequential numbering across entire chapter exam Q1 through Q10
            for q_num in range(1, 11):
                assert f"### Q{q_num} |" in result["combined_exam_markdown"], f"Missing ### Q{q_num} in combined exam"
            assert "### Q11 |" not in result["combined_exam_markdown"]

    asyncio.run(_run())


def test_mock_generate_cbse_questions_quota_exact():
    """Verify that mock generator generates exactly the allocated quota for varying question counts."""
    chunk = r"\section{Faraday's Law} \mathcal{E} = -\frac{d\Phi}{dt}"
    for q_count in [1, 2, 3, 5, 6, 10, 15]:
        sel = build_requested_selection(allocated_quota=q_count, difficulty_multiplier=1.85)
        output = mock_generate_cbse_questions(
            topic_name="Faraday's Law of Induction",
            latex_chunk=chunk,
            requested_selection=sel,
            allocated_quota=q_count,
            start_q_index=1,
        )
        matches = re.findall(r"###\s*Q(\d+)", output)
        assert len(matches) == q_count, f"Expected {q_count} questions, got {len(matches)}"
        assert [int(m) for m in matches] == list(range(1, q_count + 1))


def test_fetch_prompt_template_handles_full_and_base_urls():
    """Verify fetch_prompt_template works whether given base URL, base URL with trailing slash, or full endpoint URL."""
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        return httpx.Response(200, json={
            "success": True,
            "systemPrompt": CBSE_SYSTEM_PROMPT,
            "userPromptTemplate": CBSE_USER_PROMPT_TEMPLATE,
        })

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            # 1. Base URL without trailing slash
            r1 = await fetch_prompt_template(base_url="http://mock-server:3000", client=client)
            assert r1["source"] == "remote_api"

            # 2. Base URL with trailing slash
            r2 = await fetch_prompt_template(base_url="http://mock-server:3000/", client=client)
            assert r2["source"] == "remote_api"

            # 3. Full endpoint URL
            r3 = await fetch_prompt_template(base_url="http://mock-server:3000/api/prompt-template", client=client)
            assert r3["source"] == "remote_api"

            # All requests should have targeted exactly http://mock-server:3000/api/prompt-template
            for u in requested_urls:
                assert u == "http://mock-server:3000/api/prompt-template"

    asyncio.run(_run())


def test_render_cbse_user_prompt_with_custom_template():
    """Verify render_cbse_user_prompt respects a custom template string."""
    custom_tpl = "Custom template: {subject} - {grade} - {topic_name} (weight: {topic_weight}, quota: {allocated_quota})"
    prompt = render_cbse_user_prompt(
        subject="Chemistry",
        grade="12",
        chapter_title="Electrochemistry",
        topic_name="Nernst Equation",
        topic_weight=0.45,
        allocated_quota=3,
        latex_chunk=r"E = E^\circ - \frac{RT}{nF}\ln Q",
        requested_selection="1 MCQ, 2 SA",
        template=custom_tpl,
    )
    assert prompt == "Custom template: Chemistry - 12 - Nernst Equation (weight: 0.45, quota: 3)"


def test_generate_chapter_questions_skips_zero_quota():
    """Verify generate_chapter_questions skips topics with allocated_quota <= 0 and maintains numbering."""
    manifest = {
        "chapter_total_questions_extracted": 5,
        "target_questions": 3,
        "topics": [
            {"topic_name": "Faraday's Law of Induction", "final_weight": 0.70, "allocated_quota": 2, "difficulty_multiplier": 1.8},
            {"topic_name": "Lenz's Law", "final_weight": 0.00, "allocated_quota": 0, "difficulty_multiplier": 1.0},
            {"topic_name": "Eddy Currents", "final_weight": 0.30, "allocated_quota": 1, "difficulty_multiplier": 1.4},
        ],
    }
    chunks = [
        {"topic": "Faraday's Law of Induction", "text": "Faraday flux law"},
        {"topic": "Lenz's Law", "text": "Lenz direction"},
        {"topic": "Eddy Currents", "text": "Eddy damping"},
    ]

    async def _run():
        res = await generate_chapter_questions(
            manifest=manifest,
            chunks=chunks,
            use_mock=True,
        )
        assert res["total_questions_generated"] == 3
        assert "### Q1" in res["combined_exam_markdown"]
        assert "### Q2" in res["combined_exam_markdown"]
        assert "### Q3" in res["combined_exam_markdown"]
        assert "### Q4" not in res["combined_exam_markdown"]
        assert "Lenz's Law" not in res["combined_exam_markdown"]

    asyncio.run(_run())


def test_chunk_latex_document_uploaded_file():
    """Verify that chunk_latex_document splits solutions_textbook_1to1.tex into 19 topic-aligned chunks."""
    possible_paths = [
        os.path.join(os.getcwd(), "solutions_textbook_1to1.tex"),
        os.path.join(os.path.dirname(__file__), "..", "..", "solutions_textbook_1to1.tex"),
    ]
    target_path = next((p for p in possible_paths if os.path.exists(p)), None)
    if not target_path:
        pytest.skip("solutions_textbook_1to1.tex not present in workspace")

    with open(target_path, "r", encoding="utf-8") as f:
        chunks = chunk_latex_document(f.read())

    assert len(chunks) == 19
    numbers = [c["number"] for c in chunks]
    assert numbers == [
        "1.1", "1.2", "1.3", "1.3.1", "1.3.2",
        "1.4", "1.4.1", "1.4.2", "1.4.3",
        "1.5", "1.5.1", "1.5.2",
        "1.6", "1.6.1", "1.6.2", "1.6.3", "1.6.4", "1.6.5",
        "1.7",
    ]
    assert all(len(c["text"]) > 100 for c in chunks)


def test_normalize_hierarchical_toc_uploaded_file():
    """Verify that normalize_toc_topics traverses nested TOC hierarchy and filters out non-topic meta sections."""
    possible_paths = [
        os.path.join(os.getcwd(), "toc_result.json"),
        os.path.join(os.path.dirname(__file__), "..", "..", "toc_result.json"),
    ]
    target_path = next((p for p in possible_paths if os.path.exists(p)), None)
    if not target_path:
        pytest.skip("toc_result.json not present in workspace")

    with open(target_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    topics = normalize_toc_topics(data)
    assert len(topics) == 19
    assert topics[0] == "1.1 Types of Solutions"
    assert topics[-1] == "1.7 Abnormal Molar Masses"
    # Ensure meta-sections are excluded
    assert not any("intext question" in t.lower() for t in topics)
    assert not any("summary" in t.lower() for t in topics)
    assert not any("exercises" in t.lower() for t in topics)


def test_normalize_rich_questions_and_records():
    """Verify normalize_questions formats text and normalize_question_records retains question metadata."""
    possible_paths = [
        os.path.join(os.getcwd(), "notebook_question_bank.json"),
        os.path.join(os.path.dirname(__file__), "..", "..", "notebook_question_bank.json"),
    ]
    target_path = next((p for p in possible_paths if os.path.exists(p)), None)
    if not target_path:
        pytest.skip("notebook_question_bank.json not present in workspace")

    with open(target_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    questions = normalize_questions(data)
    assert len(questions) == 194
    assert "Camphor dissolved in nitrogen gas" in questions[0]
    # Check that MCQ options are included
    assert "A." in questions[0] and "B." in questions[0]

    records = normalize_question_records(data)
    assert len(records) == 194
    assert records[0]["topic"] == "1.1"
    assert records[0]["type"] == "mcq"
    derivations = sum(1 for r in records if r["isDerivation"])
    numericals = sum(1 for r in records if r["isNumerical"])
    assert derivations == 7
    assert numericals == 24


def test_end_to_end_uploaded_files_cycle():
    """Verify full end-to-end calculation cycle using the 3 uploaded input files."""
    possible_workspace = [
        os.getcwd(),
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
    ]
    chunks_path = None
    toc_path = None
    questions_path = None

    for ws in possible_workspace:
        c = os.path.join(ws, "solutions_textbook_1to1.tex")
        t = os.path.join(ws, "toc_result.json")
        q = os.path.join(ws, "notebook_question_bank.json")
        if os.path.exists(c) and os.path.exists(t) and os.path.exists(q):
            chunks_path, toc_path, questions_path = c, t, q
            break

    if not chunks_path:
        pytest.skip("Uploaded input files not found in workspace")

    async def _run():
        manifest = await compute_chapter_weights(
            chunks_endpoint=chunks_path,
            toc_endpoint=toc_path,
            questions_endpoint=questions_path,
            target_questions=20,
            mock_mode=True,
        )

        assert manifest["chapter_total_questions_extracted"] == 194
        assert len(manifest["topics"]) == 19
        assert manifest["target_questions"] == 20

        # Weights must sum to ~1.0
        total_weight = sum(t["final_weight"] for t in manifest["topics"])
        assert 0.999 <= total_weight <= 1.001

        # Quotas must sum exactly to 20
        quotas = [t["allocated_quota"] for t in manifest["topics"]]
        assert sum(quotas) == 20

        # Verify auto-detected chapter metadata
        assert "Chemistry" in manifest.get("subject", "")
        assert manifest.get("grade") == "12"

    asyncio.run(_run())


def test_stem_rigor_scoring():
    """Verify STEM technical rigor scores assign proper difficulty multipliers across subjects."""
    # EMI benchmarks preserved
    assert mock_score_topic_difficulty("Faraday's Law of Induction", "") == 1.85
    assert mock_score_topic_difficulty("Lenz's Law", "") == 1.25
    assert mock_score_topic_difficulty("Eddy Currents", "") == 1.40

    # Chemistry Topics
    intro = mock_score_topic_difficulty("1.1 Types of Solutions", "descriptive definitions and homogeneous mixture")
    assert intro <= 1.40

    adv_cryo = mock_score_topic_difficulty("1.6.3 Depression of Freezing Point", r"\Delta T_f = K_f m, \text{Cryoscopic constant}")
    assert adv_cryo >= 1.75

    abnormal = mock_score_topic_difficulty("1.7 Abnormal Molar Masses", r"van 't Hoff factor i = 1 - \alpha + n\alpha")
    assert abnormal >= 1.75


def test_circuit_breaker_offline_ollama():
    """Verify that when Ollama is offline, compute_chapter_weights triggers fast circuit breaker without hanging."""
    import time
    start = time.perf_counter()

    async def _run():
        manifest = await compute_chapter_weights(
            chunks_endpoint="http://127.0.0.1:59999/api/chunks",  # Non-existent port
            toc_endpoint="http://127.0.0.1:59999/api/toc",
            questions_endpoint="http://127.0.0.1:59999/api/questions",
            ollama_url="http://127.0.0.1:59999/v1/systemone",     # Non-existent port
            mock_mode=False,
            concurrency=4,
        )
        assert manifest["chapter_total_questions_extracted"] in (12, 194)

    asyncio.run(_run())
    elapsed = time.perf_counter() - start
    # Must complete fast (under 3.0 seconds) thanks to the circuit breaker
    assert elapsed < 3.0


def test_ingestion_cache_avoids_repeated_disk_reads():
    """Verify that _fetch_with_fallback populates and reuses _INGESTION_CACHE without re-reading."""
    clear_ingestion_cache()
    assert len(_INGESTION_CACHE) == 0

    async def _run():
        async with httpx.AsyncClient() as client:
            # Ingest toc_result.json
            res1 = await _fetch_with_fallback(client, "toc_result.json", "toc")
            cache_keys = [k for k in _INGESTION_CACHE.keys() if "toc" in k]
            assert len(cache_keys) > 0

            # Verify subsequent fetch returns exact same object in memory
            res2 = await _fetch_with_fallback(client, "toc_result.json", "toc")
            assert res1 is res2

            # Ingest solutions_textbook_1to1.tex chunks
            c1 = await _fetch_with_fallback(client, "solutions_textbook_1to1.tex", "chunks")
            c2 = await _fetch_with_fallback(client, "solutions_textbook_1to1.tex", "chunks")
            assert c1 is c2

            # Clear cache
            clear_ingestion_cache()
            assert len(_INGESTION_CACHE) == 0

    asyncio.run(_run())


def test_mock_generate_cbse_questions_clean_latex_no_control_characters():
    """Verify that mock generator produces pristine LaTeX math without ASCII control characters (\\x0c, \\x07, \\t)."""
    sample_topics = [
        "1.1 Types of Solutions",
        "1.3.1 Solubility of a Solid in a Liquid",
        "1.3.2 Solubility of a Gas in a Liquid",
        "1.4.1 Vapour Pressure of Liquid-Liquid Solutions",
        "1.6.2 Elevation of Boiling Point",
        "1.7 Abnormal Molar Masses",
    ]
    for topic in sample_topics:
        out = mock_generate_cbse_questions(
            topic_name=topic,
            latex_chunk="",
            requested_selection="1 MCQ, 1 Short Answer (3M), 1 Long Answer / Derivation (5M)",
            subject="Chemistry",
            grade="12",
            chapter_title="Solutions",
            start_q_index=1,
        )
        assert "\x0c" not in out, f"Form feed character found in {topic}"
        assert "\x07" not in out, f"Bell character found in {topic}"
        assert "\t" not in out, f"Tab character found in {topic}"
        assert r"\frac" in out or r"\Delta" in out or r"\alpha" in out or r"p_1" in out or r"K_H" in out


def test_all_19_topics_unique_and_aligned_questions():
    """Verify that all 19 chapter topics generate unique target concepts with no duplicate fallback MCQs."""
    all_19_topics = [
        "1.1 Types of Solutions",
        "1.2 Expressing Concentration of Solutions",
        "1.3 Solubility",
        "1.3.1 Solubility of a Solid in a Liquid",
        "1.3.2 Solubility of a Gas in a Liquid",
        "1.4 Vapour Pressure of Liquid Solutions",
        "1.4.1 Vapour Pressure of Liquid-Liquid Solutions",
        "1.4.2 Raoult's Law as a Special Case of Henry's Law",
        "1.4.3 Vapour Pressure of Solutions of Solids in Liquids",
        "1.5 Ideal and Non-ideal Solutions",
        "1.5.1 Ideal Solutions",
        "1.5.2 Non-ideal Solutions",
        "1.6 Colligative Properties and Determination of Molar Mass",
        "1.6.1 Relative Lowering of Vapour Pressure",
        "1.6.2 Elevation of Boiling Point",
        "1.6.3 Depression of Freezing Point",
        "1.6.4 Osmosis and Osmotic Pressure",
        "1.6.5 Reverse Osmosis and Water Purification",
        "1.7 Abnormal Molar Masses",
    ]

    mcq_concepts = []
    for t in all_19_topics:
        out = mock_generate_cbse_questions(
            topic_name=t,
            latex_chunk="",
            requested_selection="1 MCQ",
            subject="Chemistry",
            grade="12",
            chapter_title="Solutions",
            start_q_index=1,
        )
        match = re.search(r"- \*\*Target Concept:\*\* (.*)", out)
        assert match is not None, f"Target concept missing for {t}"
        mcq_concepts.append(match.group(1).strip())

    assert len(mcq_concepts) == 19
    # All 19 topics must have unique target concepts
    assert len(set(mcq_concepts)) == 19, f"Duplicate target concepts found in MCQs: {mcq_concepts}"

    # Verify 1.7 Long Answer generates van 't Hoff factor derivation
    la_17 = mock_generate_cbse_questions(
        topic_name="1.7 Abnormal Molar Masses",
        latex_chunk="",
        requested_selection="1 Long Answer / Derivation (5M)",
        subject="Chemistry",
        grade="12",
        chapter_title="Solutions",
        start_q_index=1,
    )
    assert "van 't Hoff factor" in la_17
    assert "elevation of boiling point" not in la_17.lower()


def _ensure_ollama_service():
    import time
    from ollama_mock_service import is_port_in_use, start_service_in_thread
    if not is_port_in_use(11434):
        start_service_in_thread(11434)
        time.sleep(0.3)


def test_ollama_service_version_and_tags():
    """Verify that Ollama / Nimble mock service adapter provides /api/version and /api/tags."""
    _ensure_ollama_service()
    with httpx.Client(timeout=3.0) as client:
        r_ver = client.get("http://localhost:11434/api/version")
        assert r_ver.status_code == 200
        assert r_ver.json().get("version") == "0.1.32"

        r_tags = client.get("http://localhost:11434/api/tags")
        assert r_tags.status_code == 200
        models = [m["name"] for m in r_tags.json().get("models", [])]
        assert "nimble:latest" in models


def test_ollama_service_v1_systemone():
    """Verify that Ollama / Nimble /v1/systemone endpoint handles Pass 1 & Pass 2 requests."""
    _ensure_ollama_service()
    with httpx.Client(timeout=3.0) as client:
        # Pass 1: Question classification
        p1 = {
            "model": "nimble",
            "state": "Question:\nState Faraday's law of electromagnetic induction.",
            "questions": {
                "assigned_topic": {
                    "type": "choice",
                    "criteria": {"Faraday's Law of Induction": "Covers Faraday", "Lenz's Law": "Covers Lenz"}
                },
                "question_type": {
                    "type": "choice",
                    "criteria": {"factual": "def", "conceptual": "conc", "numerical": "num", "derivation": "deriv"}
                }
            }
        }
        r1 = client.post("http://localhost:11434/v1/systemone", json=p1)
        assert r1.status_code == 200
        ans1 = r1.json().get("answers", {})
        assert ans1.get("assigned_topic", {}).get("choice") == "Faraday's Law of Induction"
        assert ans1.get("question_type", {}).get("choice") == "factual"

        # Pass 2: Technical rigor scoring
        p2 = {
            "model": "nimble",
            "state": "Topic: Faraday's Law of Induction\nContent:\ne = -dPhi/dt calculus derivation",
            "questions": {
                "technical_rigor": {"type": "score", "criteria": ["Intro", "Applied", "Advanced"]}
            }
        }
        r2 = client.post("http://localhost:11434/v1/systemone", json=p2)
        assert r2.status_code == 200
        ans2 = r2.json().get("answers", {})
        assert ans2.get("technical_rigor", {}).get("score") == 1.7


def test_compute_chapter_weights_live_ollama():
    """Verify compute_chapter_weights with mock_mode=False against live http://localhost:11434/v1/systemone."""
    _ensure_ollama_service()
    async def _run():
        manifest = await compute_chapter_weights(
            ollama_url="http://localhost:11434/v1/systemone",
            mock_mode=False,
            target_questions=10,
            concurrency=4,
        )
        assert manifest["chapter_total_questions_extracted"] in (12, 194)
        assert len(manifest["topics"]) >= 3
        assert sum(t["allocated_quota"] for t in manifest["topics"]) == 10

    asyncio.run(_run())


def test_gemini_configured_key_generation():
    """Verify live CBSE question generation using the configured Gemini key."""
    async def _run():
        res = await generate_topic_questions(
            topic_name="Faraday's Law of Induction",
            latex_chunk=r"\section{Faraday} e = -d\Phi/dt",
            topic_weight=0.5945,
            allocated_quota=1,
            difficulty_multiplier=1.85,
            requested_selection="1 Objective — Multiple Choice Question (1 Mark)",
            use_mock=False,
        )
        assert res["success"] is True
        assert res["mode"] == "live"
        assert "### Q" in res["output"] or "Question" in res["output"]

    asyncio.run(_run())


def test_align_chunks_to_topics_hierarchical_coalescence():
    """Verify that parent sections with child subtopics coalesce children's text."""
    toc = ["1.3 Solubility", "1.3.1 Solubility of a Solid in a Liquid", "1.3.2 Solubility of a Gas in a Liquid"]
    chunks = [
        {"number": "1.3", "title": "Solubility", "text": "Solubility depends on nature of solute and solvent."},
        {"number": "1.3.1", "title": "Solubility of a Solid in a Liquid", "text": "Like dissolves like. Polar in polar."},
        {"number": "1.3.2", "title": "Solubility of a Gas in a Liquid", "text": "Henry's Law states p = K_H * x."},
    ]
    aligned = align_chunks_to_topics(chunks, toc)
    parent_text = aligned["1.3 Solubility"]
    assert "Solubility depends on nature" in parent_text
    assert "Like dissolves like" in parent_text
    assert "Henry's Law states" in parent_text
    # Child topics remain distinct and intact
    assert "Henry's Law states" not in aligned["1.3.1 Solubility of a Solid in a Liquid"]
    assert "Henry's Law states" in aligned["1.3.2 Solubility of a Gas in a Liquid"]


def test_render_cbse_user_prompt_with_embedded_fallback_template():
    """Verify that CBSE_USER_PROMPT_TEMPLATE correctly replaces all placeholders without residual {weight_lines}."""
    prompt = render_cbse_user_prompt(
        subject="Chemistry",
        grade="12",
        chapter_title="Solutions",
        topic_name="1.1 Types of Solutions",
        topic_weight=0.0264,
        allocated_quota=1,
        latex_chunk="Homogeneous mixtures of components.",
        requested_selection="1 Objective — MCQ (1M)",
        template=CBSE_USER_PROMPT_TEMPLATE,
    )
    assert "{weight_lines}" not in prompt
    assert "- {subject}: Chemistry" in prompt
    assert "- {grade}: 12" in prompt
    assert "- {chapter_title}: Solutions" in prompt
    assert "- {topic_name}: 1.1 Types of Solutions" in prompt
    assert "- {topic_weight}: 0.0264" in prompt
    assert "- {allocated_quota}: 1" in prompt
    assert "- {requested_selection}: 1 Objective — MCQ (1M)" in prompt
    assert "Homogeneous mixtures of components." in prompt


def test_compute_dynamic_target_questions_formula():
    """Verify dynamic question budgeting formula: Q_raw = (c1*N_sig + c2*sqrt(sum(W_k))) * (D_density/1.5)."""
    # 1. EMI-scale (3 topics, total_W ~ 33, D_density ~ 1.55) -> dynamic target = 5
    raw_weights_emi = {
        "Faraday's Law of Induction": 16.65,
        "Lenz's Law": 9.375,
        "Eddy Currents": 7.00,
    }
    impacts_emi = {
        "Faraday's Law of Induction": 8.5,
        "Lenz's Law": 7.0,
        "Eddy Currents": 4.5,
    }
    diff_emi = {
        "Faraday's Law of Induction": 1.85,
        "Lenz's Law": 1.25,
        "Eddy Currents": 1.40,
    }
    q_emi = compute_dynamic_target_questions(
        raw_weights_or_manifest=raw_weights_emi,
        topic_impacts=impacts_emi,
        difficulty_map=diff_emi,
    )
    assert q_emi == 5, f"Expected 5 for EMI, got {q_emi}"

    # 2. Large chapter scale (19 topics, 440 raw impact, D_density ~ 1.6) -> dynamic target = 21
    raw_weights_large = {f"Topic {i}": 23.0 for i in range(19)}
    impacts_large = {f"Topic {i}": 14.0 for i in range(19)}
    diff_large = {f"Topic {i}": 1.6 for i in range(19)}
    q_large = compute_dynamic_target_questions(
        raw_weights_or_manifest=raw_weights_large,
        topic_impacts=impacts_large,
        difficulty_map=diff_large,
    )
    assert q_large == 21, f"Expected 21 for large chapter, got {q_large}"


def test_compute_dynamic_target_questions_manifest_input():
    """Verify compute_dynamic_target_questions handles synthesized manifest dictionary directly."""
    manifest = {
        "chapter_total_questions_extracted": 12,
        "topics": [
            {
                "topic_name": "Faraday's Law of Induction",
                "question_impact_score": 8.5,
                "difficulty_multiplier": 1.85,
                "final_weight": 0.5042,
            },
            {
                "topic_name": "Lenz's Law",
                "question_impact_score": 7.0,
                "difficulty_multiplier": 1.25,
                "final_weight": 0.2839,
            },
            {
                "topic_name": "Eddy Currents",
                "question_impact_score": 4.5,
                "difficulty_multiplier": 1.40,
                "final_weight": 0.2119,
            },
        ],
    }
    q = compute_dynamic_target_questions(manifest)
    assert q == 5


def test_compute_dynamic_target_questions_bounds():
    """Verify sensible bounds enforcement (min_questions and max_questions)."""
    raw_weights = {"T1": 1.0, "T2": 1.0}
    # Test min_questions floor
    q_min = compute_dynamic_target_questions(raw_weights, min_questions=10)
    assert q_min == 10

    # Test max_questions ceiling
    raw_weights_huge = {f"T{i}": 1000.0 for i in range(50)}
    q_max = compute_dynamic_target_questions(raw_weights_huge, max_questions=30)
    assert q_max == 30


def test_compute_dynamic_target_questions_empty_and_zero():
    """Verify edge cases: empty input returns 0."""
    assert compute_dynamic_target_questions({}) == 0
    assert compute_dynamic_target_questions([]) == 0
    assert compute_dynamic_target_questions({"topics": []}) == 0


def test_synthesize_topic_weights_dynamic_target_flag():
    """Verify that dynamic_target=True computes dynamic questions and allocates quotas."""
    toc_topics = ["Faraday's Law of Induction", "Lenz's Law", "Eddy Currents"]
    question_results = [
        {"topic": "Faraday's Law of Induction", "type": "conceptual"},
        {"topic": "Faraday's Law of Induction", "type": "numerical"},
        {"topic": "Faraday's Law of Induction", "type": "derivation"},
        {"topic": "Faraday's Law of Induction", "type": "derivation"},
        {"topic": "Lenz's Law", "type": "factual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Lenz's Law", "type": "conceptual"},
        {"topic": "Eddy Currents", "type": "factual"},
        {"topic": "Eddy Currents", "type": "conceptual"},
        {"topic": "Eddy Currents", "type": "numerical"},
    ]
    difficulty_results = {
        "Faraday's Law of Induction": 1.85,
        "Lenz's Law": 1.25,
        "Eddy Currents": 1.40,
    }

    manifest = synthesize_topic_weights(
        toc_topics=toc_topics,
        question_results=question_results,
        difficulty_results=difficulty_results,
        dynamic_target=True,
    )

    assert manifest["is_dynamic_target"] is True
    assert manifest["target_questions_mode"] == "dynamic"
    assert manifest["target_questions"] == 5
    quotas = [t["allocated_quota"] for t in manifest["topics"]]
    assert sum(quotas) == 5
    assert quotas[0] >= quotas[1]


def test_synthesize_topic_weights_target_questions_auto():
    """Verify that target_questions='auto' activates dynamic budgeting identically."""
    toc_topics = ["Topic A", "Topic B", "Topic C"]
    question_results = [
        {"topic": "Topic A", "type": "derivation"},
        {"topic": "Topic B", "type": "factual"},
        {"topic": "Topic C", "type": "factual"},
    ]
    difficulty_results = {"Topic A": 1.5, "Topic B": 1.5, "Topic C": 1.5}

    for auto_val in ("auto", "AUTO", "dynamic", "Dynamic"):
        manifest = synthesize_topic_weights(
            toc_topics=toc_topics,
            question_results=question_results,
            difficulty_results=difficulty_results,
            target_questions=auto_val,
        )
        assert manifest["is_dynamic_target"] is True
        assert manifest["target_questions"] > 0
        quotas = [t["allocated_quota"] for t in manifest["topics"]]
        assert sum(quotas) == manifest["target_questions"]


def test_compute_chapter_weights_dynamic_target():
    """Verify compute_chapter_weights with dynamic_target=True produces valid manifest."""
    async def _test():
        manifest = await compute_chapter_weights(
            dynamic_target=True,
            mock_mode=True,
        )
        assert manifest["is_dynamic_target"] is True
        assert manifest["target_questions_mode"] == "dynamic"
        assert 20 <= manifest["target_questions"] <= 25
        assert sum(t["allocated_quota"] for t in manifest["topics"]) == manifest["target_questions"]

    asyncio.run(_test())


def test_orchestrate_and_generate_dynamic_target():
    """Verify orchestrate_and_generate with target_questions='auto' runs end-to-end."""
    async def _test():
        result = await orchestrate_and_generate(
            target_questions="auto",
            mock_mode=True,
        )
        assert result["manifest"]["is_dynamic_target"] is True
        target_q = result["manifest"]["target_questions"]
        assert 20 <= target_q <= 25
        assert result["total_questions_generated"] == target_q
        assert "combined_exam_markdown" in result

    asyncio.run(_test())


def test_compute_dynamic_target_questions_dirty_and_corrupt_inputs():
    """Verify compute_dynamic_target_questions sanitizes invalid strings, Nones, and malformed fields without crashing."""
    corrupt_manifest = {
        "chapter_total_questions_extracted": 10,
        "topics": [
            {"topic_name": "T1", "question_impact_score": "not_a_number", "difficulty_multiplier": "high"},
            {"topic_name": "T2", "question_impact_score": None, "difficulty_multiplier": None},
            {"topic_name": "T3", "question_impact_score": "", "difficulty_multiplier": "1.5"},
            {"topic_name": "T4", "question_impact_score": 12.0, "difficulty_multiplier": 1.5},
            "not_a_dict_topic",
        ],
    }
    # Should safely compute without throwing ValueError or TypeError
    q = compute_dynamic_target_questions(
        corrupt_manifest,
        c1="0.50",
        c2="0.50",
        prior="0.5",
        min_questions="4",
        max_questions="20",
    )
    assert 4 <= q <= 20

    # Test dictionary with None and string weights
    dirty_dict = {"T1": None, "T2": "invalid", "T3": 15.0}
    q_dict = compute_dynamic_target_questions(dirty_dict, min_questions=2)
    assert q_dict >= 2


def test_compute_dynamic_target_questions_topic_list_input():
    """Verify compute_dynamic_target_questions handles list of topic dicts directly."""
    topics = [
        {"topic_name": "Faraday's Law", "question_impact_score": 10.0, "difficulty_multiplier": 1.85},
        {"topic_name": "Lenz's Law", "question_impact_score": 5.0, "difficulty_multiplier": 1.25},
        {"topic_name": "Eddy Currents", "question_impact_score": 5.0, "difficulty_multiplier": 1.40},
    ]
    q = compute_dynamic_target_questions(topics)
    assert q == 5


def test_apportion_quotas_standalone():
    """Verify standalone apportion_quotas allocates Hamilton quotas accurately."""
    topics = [
        {"topic_name": "T1", "question_impact_score": 15.0, "difficulty_multiplier": 1.5},
        {"topic_name": "T2", "question_impact_score": 5.0, "difficulty_multiplier": 1.0},
    ]
    # Normal apportionment
    allocated = apportion_quotas(topics, target_questions=5)
    assert sum(t["allocated_quota"] for t in allocated) == 5
    assert allocated[0]["allocated_quota"] >= allocated[1]["allocated_quota"]

    # Target 0
    zero_alloc = apportion_quotas(topics, target_questions=0)
    assert sum(t["allocated_quota"] for t in zero_alloc) == 0

    # Negative target
    neg_alloc = apportion_quotas(topics, target_questions=-3)
    assert sum(t["allocated_quota"] for t in neg_alloc) == 0

    # Empty list
    assert apportion_quotas([], target_questions=5) == []


def test_generate_chapter_questions_dynamic_auto_manifest():
    """Verify generate_chapter_questions automatically resolves dynamic target and quotas when passed target_questions='auto'."""
    async def _test():
        raw_manifest = {
            "target_questions": "auto",
            "topics": [
                {"topic_name": "Faraday's Law of Induction", "question_impact_score": 10.0, "difficulty_multiplier": 1.85, "final_weight": 0.5945},
                {"topic_name": "Lenz's Law", "question_impact_score": 4.5, "difficulty_multiplier": 1.25, "final_weight": 0.1913},
                {"topic_name": "Eddy Currents", "question_impact_score": 4.5, "difficulty_multiplier": 1.40, "final_weight": 0.2142},
            ],
        }
        res = await generate_chapter_questions(
            manifest=raw_manifest,
            chunks=[],
            use_mock=True,
        )
        assert res["manifest"]["is_dynamic_target"] is True
        assert res["manifest"]["target_questions"] == 5
        assert res["total_questions_generated"] == 5
        # Ensure header does not contain literal string '**Target Questions:** auto'
        assert "**Target Questions:** 5" in res["combined_exam_markdown"]
        assert "**Target Questions:** auto" not in res["combined_exam_markdown"]

    asyncio.run(_test())


def test_cli_min_max_bounds_and_validation(monkeypatch):
    """Verify CLI argument parsing accepts min/max bounds and rejects negative target questions."""
    # Test valid args with --min-questions and --max-questions
    test_args = ["orchestrator.py", "--dynamic-target", "--min-questions", "8", "--max-questions", "25"]
    monkeypatch.setattr("sys.argv", test_args)
    parsed = parse_args()
    assert parsed.dynamic_target is True
    assert parsed.min_questions == 8
    assert parsed.max_questions == 25

    # Test auto target string parsing
    test_args_auto = ["orchestrator.py", "--target-questions", "auto"]
    monkeypatch.setattr("sys.argv", test_args_auto)
    parsed_auto = parse_args()
    assert parsed_auto.target_questions == "auto"

    # Test negative target questions rejection
    test_args_neg = ["orchestrator.py", "--target-questions", "-5"]
    monkeypatch.setattr("sys.argv", test_args_neg)
    with pytest.raises(SystemExit):
        parse_args()


def test_extreme_boundary_and_degeneracy():
    """Verify extreme boundary conditions: all zero weights, negative weights, and extreme difficulties."""
    # All zero weights with min_questions=3
    zero_weights = {"T1": 0.0, "T2": 0.0}
    assert compute_dynamic_target_questions(zero_weights, min_questions=3) == 3

    # All negative weights clamp safely
    neg_weights = {"T1": -10.0, "T2": -5.0}
    assert compute_dynamic_target_questions(neg_weights, min_questions=2) == 2

    # Negative min_questions clamps to 0 minimum
    assert compute_dynamic_target_questions(zero_weights, min_questions=-5) == 0


def test_validate_target_questions_helper():
    """Verify validate_target_questions helper logic for dynamic mode, range [10, 50], and clamping."""
    # Dynamic inputs
    assert validate_target_questions(None) == (None, True)
    assert validate_target_questions("auto") == (None, True)
    assert validate_target_questions("dynamic") == (None, True)
    assert validate_target_questions("AUTO") == (None, True)

    # Valid manual inputs
    assert validate_target_questions(10) == (10, False)
    assert validate_target_questions(25) == (25, False)
    assert validate_target_questions(50) == (50, False)
    assert validate_target_questions("30") == (30, False)

    # Clamping behavior
    assert validate_target_questions(4, clamp=True) == (MIN_MANUAL_QUESTIONS, False)
    assert validate_target_questions(0, clamp=True) == (MIN_MANUAL_QUESTIONS, False)
    assert validate_target_questions(-10, clamp=True) == (MIN_MANUAL_QUESTIONS, False)
    assert validate_target_questions(75, clamp=True) == (MAX_MANUAL_QUESTIONS, False)

    # Strict error behavior
    with pytest.raises(ValueError, match="Manual target questions must be between"):
        validate_target_questions(5, clamp=False)

    with pytest.raises(ValueError, match="Manual target questions must be between"):
        validate_target_questions(60, clamp=False)


def test_dual_mode_compute_chapter_weights_default_dynamic():
    """Verify compute_chapter_weights defaults to fully dynamic mode when no target is passed."""
    async def _test():
        manifest = await compute_chapter_weights(mock_mode=True)
        assert manifest["is_dynamic_target"] is True
        assert manifest["target_questions_mode"] == "dynamic"
        assert 20 <= manifest["target_questions"] <= 25
        assert sum(t["allocated_quota"] for t in manifest["topics"]) == manifest["target_questions"]

    asyncio.run(_test())


def test_dual_mode_compute_chapter_weights_manual_selection():
    """Verify compute_chapter_weights with manual choice between 10 and 50 fixes total while keeping allocation dynamic."""
    async def _test():
        manifest = await compute_chapter_weights(
            target_questions=25,
            mock_mode=True,
        )
        assert manifest["is_dynamic_target"] is False
        assert manifest["target_questions_mode"] == "manual"
        assert manifest["target_questions"] == 25
        quotas = [t["allocated_quota"] for t in manifest["topics"]]
        assert sum(quotas) == 25

        # Topic with highest weight receives larger quota than topic with low weight
        sorted_topics = sorted(manifest["topics"], key=lambda t: t["final_weight"], reverse=True)
        assert sorted_topics[0]["allocated_quota"] > sorted_topics[-1]["allocated_quota"]

    asyncio.run(_test())


def test_dual_mode_compute_chapter_weights_clamping():
    """Verify compute_chapter_weights clamps manual total outside [10, 50]."""
    async def _test():
        # Below minimum range: 4 clamped to 10
        manifest_low = await compute_chapter_weights(
            target_questions=4,
            mock_mode=True,
            clamp_manual=True,
        )
        assert manifest_low["target_questions"] == 10
        assert manifest_low["target_questions_mode"] == "manual"
        assert sum(t["allocated_quota"] for t in manifest_low["topics"]) == 10

        # Above maximum range: 75 clamped to 50
        manifest_high = await compute_chapter_weights(
            target_questions=75,
            mock_mode=True,
            clamp_manual=True,
        )
        assert manifest_high["target_questions"] == 50
        assert manifest_high["target_questions_mode"] == "manual"
        assert sum(t["allocated_quota"] for t in manifest_high["topics"]) == 50

    asyncio.run(_test())


def test_dual_mode_orchestrate_and_generate_default_dynamic():
    """Verify orchestrate_and_generate without arguments defaults to dynamic mode."""
    async def _test():
        result = await orchestrate_and_generate(mock_mode=True)
        manifest = result["manifest"]
        assert manifest["is_dynamic_target"] is True
        assert manifest["target_questions_mode"] == "dynamic"
        assert result["total_questions_generated"] == manifest["target_questions"]

    asyncio.run(_test())


def test_dual_mode_orchestrate_and_generate_manual():
    """Verify orchestrate_and_generate with explicit target between 10 and 50 fixes question total."""
    async def _test():
        result = await orchestrate_and_generate(
            target_questions=15,
            mock_mode=True,
        )
        manifest = result["manifest"]
        assert manifest["is_dynamic_target"] is False
        assert manifest["target_questions_mode"] == "manual"
        assert manifest["target_questions"] == 15
        assert result["total_questions_generated"] == 15
        assert "**Target Questions:** 15" in result["combined_exam_markdown"]

    asyncio.run(_test())


def test_cli_dual_mode_defaults_and_validation(monkeypatch):
    """Verify CLI defaults to auto dynamic mode and enforces [10, 50] range for manual choice."""
    # CLI without args defaults to dynamic "auto"
    monkeypatch.setattr("sys.argv", ["orchestrator.py"])
    parsed = parse_args()
    assert parsed.target_questions == "auto"

    # Explicit manual selection within [10, 50]
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "--target-questions", "35"])
    parsed_manual = parse_args()
    assert parsed_manual.target_questions == 35

    # Lower boundary 10
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "--target-questions", "10"])
    assert parse_args().target_questions == 10

    # Upper boundary 50
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "--target-questions", "50"])
    assert parse_args().target_questions == 50

    # Below 10 -> SystemExit
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "--target-questions", "9"])
    with pytest.raises(SystemExit):
        parse_args()

    # Above 50 -> SystemExit
    monkeypatch.setattr("sys.argv", ["orchestrator.py", "--target-questions", "51"])
    with pytest.raises(SystemExit):
        parse_args()


def test_dual_mode_unclamped_raises_error():
    """Verify compute_chapter_weights and orchestrate_and_generate raise ValueError when clamp_manual=False and target is out of range."""
    async def _test():
        # Below 10 with clamp_manual=False raises ValueError
        with pytest.raises(ValueError, match="Manual target questions must be between"):
            await compute_chapter_weights(
                target_questions=5,
                mock_mode=True,
                clamp_manual=False,
            )

        # Above 50 with clamp_manual=False raises ValueError
        with pytest.raises(ValueError, match="Manual target questions must be between"):
            await compute_chapter_weights(
                target_questions=65,
                mock_mode=True,
                clamp_manual=False,
            )

        # orchestrate_and_generate also raises ValueError
        with pytest.raises(ValueError, match="Manual target questions must be between"):
            await orchestrate_and_generate(
                target_questions=5,
                mock_mode=True,
                clamp_manual=False,
            )

    asyncio.run(_test())


def test_generate_chapter_questions_unallocated_manifest_with_manual_target():
    """Verify generate_chapter_questions respects manual target_questions even when manifest topics lack allocated_quota."""
    async def _test():
        manifest = {
            "target_questions": 25,
            "topics": [
                {"topic_name": "Faraday's Law of Induction", "final_weight": 0.6},
                {"topic_name": "Lenz's Law", "final_weight": 0.4},
            ],
        }
        result = await generate_chapter_questions(
            manifest=manifest,
            chunks=[],
            use_mock=True,
        )
        assert manifest["target_questions"] == 25
        assert manifest["target_questions_mode"] == "manual"
        assert manifest["is_dynamic_target"] is False
        assert sum(t["allocated_quota"] for t in manifest["topics"]) == 25
        assert result["total_questions_generated"] == 25

    asyncio.run(_test())


def test_validate_target_questions_edge_cases():
    """Verify validate_target_questions handles boolean, float-strings, and non-numeric inputs correctly."""
    # Booleans should resolve to dynamic, not numeric 1 or 0
    assert validate_target_questions(True) == (None, True)
    assert validate_target_questions(False) == (None, True)

    # Float strings like "25.0" should parse to integer 25 in manual mode
    assert validate_target_questions("25.0") == (25, False)
    assert validate_target_questions(25.4) == (25, False)

    # Non-numeric with clamp=False raises ValueError
    with pytest.raises(ValueError, match="Invalid manual target-questions value"):
        validate_target_questions("not_a_number", clamp=False)



