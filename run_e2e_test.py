"""
End-to-end verification script for:
1. Nimble topic weight & difficulty calculation across 19 topics and 194 questions (via port 11434).
2. Live Google Gemini question generation using direct_gemini / live mode.
3. Measurement and recording of all metrics, weights, quotas, questions, and CBSE marking schemes.
"""

import asyncio
import json
import os
import sys
import time

# Ensure proper utf-8 output encoding on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

from orchestrator import (
    compute_chapter_weights,
    generate_chapter_questions,
    fetch_prompt_template,
    normalize_chunks,
    DEFAULT_OLLAMA_SYSTEMONE_URL,
    DEFAULT_GEMINI_API_KEY,
)
from ollama_mock_service import is_port_in_use, start_service_in_thread


from typing import Union


def ensure_ollama_active():
    if not is_port_in_use(11434):
        print("Starting Ollama/Nimble mock service on port 11434...")
        start_service_in_thread(11434)
        time.sleep(0.5)
    print("Ollama/Nimble service on port 11434 is ACTIVE.")


async def run_e2e(target_questions: Union[int, str] = "auto"):
    ensure_ollama_active()

    print("=" * 80)
    print("STARTING END-TO-END PIPELINE: NIMBLE WEIGHTS + GEMINI LIVE QUESTION GENERATION")
    print(f"Inputs: toc_result.json, solutions_textbook_1to1.tex, notebook_question_bank.json")
    print(f"Target Questions: {target_questions}")
    print("=" * 80)

    total_start = time.perf_counter()

    # ----------------- PASS 1 & 2: NIMBLE WEIGHT CALCULATION -----------------
    print("\n>>> STEP 1: Running Nimble Weight & Rigor Calculation...")
    nimble_start = time.perf_counter()

    manifest = await compute_chapter_weights(
        chunks_endpoint="solutions_textbook_1to1.tex",
        toc_endpoint="toc_result.json",
        questions_endpoint="notebook_question_bank.json",
        ollama_url="http://localhost:11434/v1/systemone",
        mock_mode=False,
        concurrency=16,
        target_questions=target_questions,
    )
    nimble_end = time.perf_counter()
    nimble_time = nimble_end - nimble_start

    print(f"Nimble Calculation Completed in {nimble_time:.3f} seconds.")
    print(f"Chapter: {manifest.get('chapter_title')}")
    print(f"Subject: {manifest.get('subject')} | Grade: {manifest.get('grade')}")
    print(f"Total Extracted Questions Classified: {manifest.get('chapter_total_questions_extracted')}")
    print(f"Total Topics Scored: {len(manifest.get('topics', []))}")

    # ----------------- STEP 2: LOAD CHUNKS & PROMPT TEMPLATE -----------------
    print("\n>>> STEP 2: Ingesting Chunks & CBSE Prompt Template...")
    with open("solutions_textbook_1to1.tex", "r", encoding="utf-8") as f:
        tex_content = f.read()
    chunks = normalize_chunks(tex_content)
    prompt_template = await fetch_prompt_template(base_url="http://localhost:3000/api/prompt-template")

    # ----------------- STEP 3: GEMINI LIVE QUESTION GENERATION -----------------
    print("\n>>> STEP 3: Running Live Google Gemini Question Generation (direct_gemini / live mode)...")
    gemini_start = time.perf_counter()

    exam_result = await generate_chapter_questions(
        manifest=manifest,
        chunks=chunks,
        subject=manifest.get("subject", "Chemistry"),
        grade=manifest.get("grade", "12"),
        chapter_title=manifest.get("chapter_title", "Class 12 Chemistry - Unit 1: Solutions"),
        prompt_template=prompt_template,
        api_url=None,  # Force direct_gemini live mode
        api_key=DEFAULT_GEMINI_API_KEY,
        use_mock=False,
    )

    gemini_end = time.perf_counter()
    gemini_time = gemini_end - gemini_start
    total_time = gemini_end - total_start

    print(f"Gemini Question Generation Completed in {gemini_time:.3f} seconds.")
    print(f"Total Pipeline Execution Time: {total_time:.3f} seconds.")

    # ----------------- RESULTS SUMMARY -----------------
    output_data = {
        "timing": {
            "nimble_weight_calculation_seconds": round(nimble_time, 3),
            "gemini_generation_seconds": round(gemini_time, 3),
            "total_execution_seconds": round(total_time, 3),
        },
        "manifest": manifest,
        "total_questions_generated": exam_result.get("total_questions_generated"),
        "topics_generated": len(exam_result.get("topics", [])),
        "exam_result": exam_result,
    }

    # Save complete run artifact
    with open("e2e_run_output.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)

    with open("e2e_generated_exam.md", "w", encoding="utf-8") as f:
        f.write(exam_result.get("combined_exam_markdown", ""))

    print("\n" + "=" * 80)
    print("ALL RUN DATA AND EXAM MARKDOWN SUCCESSFULLY GENERATED AND SAVED!")
    print("Files saved: e2e_run_output.json, e2e_generated_exam.md")
    print("=" * 80)

    return output_data


if __name__ == "__main__":
    raw_arg = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if str(raw_arg).lower() in ("auto", "dynamic"):
        tq = "auto"
    else:
        try:
            tq = int(raw_arg)
        except ValueError:
            tq = "auto"
    asyncio.run(run_e2e(tq))
