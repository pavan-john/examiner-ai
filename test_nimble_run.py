import asyncio
import time
from orchestrator import compute_chapter_weights

async def main():
    t0 = time.perf_counter()
    manifest = await compute_chapter_weights(
        chunks_endpoint="solutions_textbook_1to1.tex",
        toc_endpoint="toc_result.json",
        questions_endpoint="notebook_question_bank.json",
        ollama_url="http://localhost:11434/v1/systemone",
        mock_mode=False,
        concurrency=16,
        target_questions="auto",
        dynamic_target=True,
    )
    t1 = time.perf_counter()
    print(f"Pass 1 & 2 Execution Time: {t1 - t0:.3f} seconds")
    print(f"Chapter Title: {manifest.get('chapter_title')}")
    print(f"Subject: {manifest.get('subject')} | Grade: {manifest.get('grade')}")
    print(f"Total questions extracted: {manifest.get('chapter_total_questions_extracted')}")
    print(f"Number of topics: {len(manifest.get('topics', []))}")
    print(f"Total target questions: {manifest.get('target_questions')}")
    print("\n--- Topic Weights & Allocations ---")
    for t in manifest["topics"]:
        print(f"Topic: {t['topic_name']}")
        print(f"  Questions: {t['existing_questions']}, Impact: {t['question_impact_score']}, Diff: {t['difficulty_multiplier']}, Weight: {t['final_weight']}, Quota: {t['allocated_quota']}")

if __name__ == "__main__":
    asyncio.run(main())
