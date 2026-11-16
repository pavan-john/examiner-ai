# CBSE Question Generator & Topic Weight Orchestration Pipeline

An end-to-end intelligent assessment generation platform for CBSE Senior Secondary (Classes 10, 11, and 12). The system integrates **Ollama / Nimble System 1** for topic weighting and syllabus importance calculations with **Google Gemini Flash** for generating authentic CBSE board examination questions, step-by-step marking schemes, and multi-tier cognitive blueprints.

---

## Architecture & Core Components

```
├── cbse-generator/                  # Web Application & API Server
│   ├── public/                      # Web UI (Tailwind CSS, KaTeX, Lucide Icons)
│   │   ├── index.html               # Main dashboard with Dynamic / Manual modes
│   │   └── app.js                   # Client controller and state manager
│   ├── server.js                    # Express API server (Gemini Flash & Mock engine)
│   ├── orchestrator.py              # Chapter weight orchestration engine
│   ├── test_api.js                  # Integration test suite (40 tests)
│   └── tests/                       # Pytest test suite (71 tests)
├── orchestrator.py                  # Topic-wise syllabus weighting & quota allocator
├── ollama_mock_service.py           # Nimble System 1 /v1/systemone fallback service
├── generate_pdf_report.py           # Comprehensive PDF report generator (ReportLab)
├── start.bat                        # One-click startup for all services
├── stop.bat                         # Clean service shutdown script
├── solutions_textbook_1to1.tex      # Authentic Class 12 LaTeX textbook source
├── notebook_question_bank.json      # Reference exemplar question bank
└── toc_result.json                  # Chapter table of contents metadata
```

---

## Key Features

1. **Dynamic Question Taxonomy Allocation (Default)**:
   - Evaluates topic syllabus weight ($P_j$) and technical difficulty ($D_j$).
   - High-rigor topics receive derivations (5M), case studies (4M), and multi-step numericals (3M).
   - Conceptual topics receive MCQs (1M), Assertion-Reasoning (1M), and Very Short Answers (2M).
   - **Zero-click requirement**: Questions are dynamically apportioned without requiring manual stepper clicks.

2. **Dual-Mode Total Question Budgeting**:
   - **Dynamic Target (Default)**: Automatically determines the optimal exam size (10–25 questions) based on content volume and formula density.
   - **Manual Target (10–50 Qs)**: Allows custom question counts while keeping topic quotas proportional to weights.

3. **Multi-Model Gemini Flash Cascade**:
   - Direct integration with Google Gemini Flash models (`gemini-flash-lite-latest`, `gemini-2.5-flash`, `gemini-1.5-flash`).
   - Built-in Mock Mode for testing all workflows without an API key.

4. **CBSE Question Formats Supported**:
   - Section A: Objective MCQs (1M) & Assertion-Reason (1M) with official 4-code format.
   - Section B: Very Short Answer (2M).
   - Section C: Short Answer (3M).
   - Section D: Case-Based / Source-Based (4M).
   - Section E: Long Answer / First-principles Derivations (5M).

---

## Quick Start

### Prerequisites
- [Node.js](https://nodejs.org/) (v18+)
- [Python](https://www.python.org/) (3.10+)

### 1. Environment Setup
Copy the example environment file and add your Gemini API key:
```bash
cp .env.example .env
```

Edit `.env`:
```env
GEMINI_API_KEY=your_gemini_api_key_here
OLLAMA_SYSTEMONE_URL=http://localhost:11434/v1/systemone
PORT=3000
```

### 2. Install Dependencies
```bash
cd cbse-generator
npm install
pip install -r ../requirements.txt  # or: pip install httpx pytest reportlab
cd ..
```

### 3. Launch Services
On Windows, double-click **`start.bat`** or run:
```cmd
start.bat
```
This automatically starts:
- Nimble System 1 service on port `11434`
- CBSE Generator Web Application on `http://localhost:3000`

To stop all background services:
```cmd
stop.bat
```

---

## Running Automated Tests

### Python Pytest Suite (71 Tests)
```bash
python -m pytest cbse-generator/tests/test_orchestrator.py -v
```

### Node.js API Integration Tests (40 Tests)
```bash
node cbse-generator/test_api.js
```

---

## License
MIT License
