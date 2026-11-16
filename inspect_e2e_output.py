import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

with open("e2e_run_output.json", "r", encoding="utf-8") as f:
    data = json.load(f)

print("=== TIMING BREAKDOWN ===")
for k, v in data["timing"].items():
    print(f"  {k}: {v}s")

print(f"\nTotal questions generated: {data['total_questions_generated']}")
print(f"Total topics with generated questions: {data['topics_generated']}")

print("\n=== COMPLETE SYNTHESIZED WEIGHTS FOR ALL 19 TOPICS ===")
print(f"{'Topic Name':<55} | {'Existing Qs':<11} | {'Impact':<7} | {'Diff':<5} | {'Weight':<8} | {'Quota':<5}")
print("-" * 105)
for t in data["manifest"]["topics"]:
    print(f"{t['topic_name']:<55} | {t['existing_questions']:<11} | {t['question_impact_score']:<7.2f} | {t['difficulty_multiplier']:<5.2f} | {t['final_weight']:<8.4f} | {t['allocated_quota']:<5}")

print("\n=== GENERATED TOPIC SESSIONS (MODE, SOURCE, MODEL) ===")
for top in data["exam_result"]["topics"]:
    print(f"Topic: {top['topic_name']}")
    print(f"  Quota: {top['allocated_quota']} | Requested: {top['requested_selection']}")
    print(f"  Mode: {top['mode']} | Source: {top['source']} | Model: {top.get('model')}")
    output_lines = [l for l in top.get('output', '').split('\n') if l.strip()]
    first_few = '\n    '.join(output_lines[:4])
    print(f"  Preview:\n    {first_few}\n")
