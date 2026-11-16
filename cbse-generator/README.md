# CBSE Question Generator (Topic-Wise LaTeX Pipeline)

A specialized Proof of Concept (POC) web application designed for CBSE Senior Secondary (Classes 10, 11, and 12) paper setters and NCERT specialists. The application ingests topic-wise raw LaTeX textbook chunks, parses syllabus metadata, accepts fine-grained question specifications (1M, 2M, 3M, 4M, 5M), and orchestrates Google Gemini Flash to generate authentic CBSE board examination questions with step-by-step marking schemes.

---

## Features

1. **Topic-Wise LaTeX Chunk Ingestion**:
   - Upload `.tex` or `.txt` chapter files.
   - Built-in automated section chunker: detects `\section{...}` and `\subsection{...}` and isolates topic chunks with one click.
   - Direct raw LaTeX code editor with live character count.
   - Pre-loaded with 3 authentic NCERT Class 12 topic chunks:
     - **Physics**: *Ray Optics & Optical Instruments* — Refraction through a Prism
     - **Chemistry**: *Electrochemistry* — Nernst Equation & Equilibrium Constant
     - **Mathematics**: *Integrals* — Integration by Parts & ILATE rule

2. **CBSE Question Taxonomy & Blueprint Selector**:
   - **1 Mark**: Objective Multiple Choice Questions (MCQs) with distractors
   - **1 Mark**: Assertion-Reason (A-R) with official 4-code format (a, b, c, d)
   - **2 Marks**: Very Short Answer (VSA) (definitions, laws, SI units)
   - **3 Marks**: Short Answer (SA) (derivations, principles, 2-step numericals)
   - **4 Marks**: Case-Based / Source-Based (passage + sub-questions with internal OR)
   - **5 Marks**: Long Answer (LA) / Derivations (first-principles proofs & multi-step numericals)
   - Quick Blueprint Presets: *Standard Board Slice (11M)*, *Numericals & Derivations (16M)*, *Case Studies Only (8M)*, *1M Objective Drill (6M)*.

3. **Gemini Flash LLM Integration**:
   - Strict adherence to the NCERT/CBSE Paper Setter system context and blueprint contract.
   - Feasibility filter check (`[SKIPPED: ...]` if a requested format lacks mathematical/conceptual basis in the chunk).
   - Diagram Setup textual description generator for apparatus/figures.
   - Dynamic prompt compilation injecting `{subject}`, `{grade}`, `{chapter_title}`, `{topic_name}`, `{latex_chunk}`, and `{requested_selection}`.
   - Direct integration with `gemini-2.5-flash` and `gemini-1.5-flash`.
   - Built-in Mock Mode: Test the entire UI and workflows immediately even without an API key!
   - API Key modal to enter and test your Google Gemini API key.

4. **Exam Paper & Presentation Tools**:
   - Typeset mathematical equations with KaTeX (`$ ... $` inline, `$$ ... $$` display).
   - 🔴 **Tier 1 (Core / Must-Know)** and 🟡 **Tier 2 (Higher Order / Differentiator)** visual badges.
   - Collapsible Step-Wise Marking Schemes with marks breakdown badges (`[1/2 M]`, `[1 M]`).
   - **Student Mode** (hide solutions for test-taking) vs. **Teacher Mode** (show marking schemes).
   - **Print to PDF** formatted as an official CBSE Board Examination paper.
   - One-click **Copy as Markdown**.

---

## Quick Start

### 1. Start the Server
```bash
cd cbse-generator
npm start
```
The server will start on: **`http://localhost:3000`**

### 2. Open in Browser
Visit `http://localhost:3000` in any web browser.

### 3. Usage
- Click **"Load Sample"** to cycle through Physics, Chemistry, and Mathematics topics, or upload your own `.tex` file.
- Configure question counters or pick a preset like **"Standard Board Slice"**.
- Click **"Generate CBSE Questions"**.
- To use your live Gemini Flash API key: click **"Gemini API Key"** in the top navigation bar, enter your key, click **"Test Key"**, and save.
