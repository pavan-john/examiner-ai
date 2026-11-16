import json
from orchestrator import (
    normalize_toc_topics,
    normalize_chunks,
    chunk_latex_document,
    align_chunks_to_topics,
    normalize_questions,
    normalize_question_records,
)

with open("toc_result.json", "r", encoding="utf-8") as f:
    toc_data = json.load(f)
toc_topics = normalize_toc_topics(toc_data)

with open("solutions_textbook_1to1.tex", "r", encoding="utf-8") as f:
    tex_data = f.read()
chunks = chunk_latex_document(tex_data)
aligned = align_chunks_to_topics(chunks, toc_topics)

print(f"Total TOC topics: {len(toc_topics)}")
print(f"Total Chunks: {len(chunks)}")
for t in toc_topics:
    text_len = len(aligned.get(t, "").strip())
    print(f"Topic: {t:50s} | Chunk text length: {text_len}")
