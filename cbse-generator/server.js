const express = require('express');
const cors = require('cors');
const fs = require('fs');
const path = require('path');

const app = express();
const PORT = process.env.PORT || 3000;

app.use(cors());
app.use(express.json({ limit: '10mb' }));
app.use(express.static(path.join(__dirname, 'public')));

// Load .env if present (check both local and parent directory)
[path.join(__dirname, '.env'), path.join(__dirname, '..', '.env')].forEach(envFile => {
  if (fs.existsSync(envFile)) {
    const envContent = fs.readFileSync(envFile, 'utf8');
    envContent.split('\n').forEach(line => {
      const match = line.match(/^\s*([\w_]+)\s*=\s*(.*)?\s*$/);
      if (match) {
        const key = match[1];
        const val = (match[2] || '').trim().replace(/^['"]|['"]$/g, '');
        if (!process.env[key]) {
          process.env[key] = val;
        }
      }
    });
  }
});
const DEFAULT_GEMINI_API_KEY = process.env.GEMINI_API_KEY || '';
if (!process.env.GEMINI_API_KEY) {
  process.env.GEMINI_API_KEY = DEFAULT_GEMINI_API_KEY;
}
console.log('Loaded .env successfully. GEMINI_API_KEY present:', !!process.env.GEMINI_API_KEY);

// Load pre-bundled samples
const samplesPath = path.join(__dirname, 'data', 'samples.json');
let samples = [];
try {
  samples = JSON.parse(fs.readFileSync(samplesPath, 'utf8'));
} catch (err) {
  console.warn('Warning: Could not read samples.json', err.message);
}

// Load chapter orchestration data
const chapterEmiPath = path.join(__dirname, 'data', 'chapter_emi.json');
let chapterData = {
  chapter: 'Electromagnetic Induction',
  toc: {
    topics: [
      "Faraday's Law of Induction",
      "Lenz's Law",
      "Eddy Currents"
    ]
  },
  chunks: [],
  questions: []
};
try {
  if (fs.existsSync(chapterEmiPath)) {
    chapterData = JSON.parse(fs.readFileSync(chapterEmiPath, 'utf8'));
  }
} catch (err) {
  console.warn('Warning: Could not read chapter_emi.json', err.message);
}

const SYSTEM_PROMPT = `# SYSTEM CONTEXT: CBSE QUESTION GENERATOR (TOPIC-WISE LATEX PIPELINE)

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
   - Look strictly at \`{requested_selection}\` and \`{allocated_quota}\`.
   - **DO NOT** generate any question type, mark value, or quantity that was not requested.
   - If the user asks for *only 1-mark MCQs*, output **only** 1-mark MCQs.
   - If the user asks for *2 questions of 3 marks*, output **exactly 2** questions of 3 marks.
   - When \`{topic_weight}\` and \`{allocated_quota}\` are provided, calibrate question cognitive depth, difficulty tier, and coverage to match the assigned weight.
2. **Chunk Feasibility Check:**
   - If a requested format is mathematically or conceptually impossible from the provided topic chunk (e.g., the user requested a "5-mark Derivation", but the chunk contains only purely descriptive qualitative definitions with zero formulas), state:
     \`[SKIPPED: {Requested Type} - Topic chunk lacks required theoretical/mathematical basis]\`
   - Do NOT invent out-of-syllabus formulas or concepts outside \`{latex_chunk}\`.

---

## 3. LATEX CHUNK PROCESSING RULES

1. **Math Formatting:** 
   - Render all mathematical equations, symbols, units, and formulas in standard LaTeX math format: inline \`$ ... $\` or display \`$$ ... $$\`.
2. **Handling Graphics / Figures (\`\\includegraphics\`, Figures):**
   - LaTeX textbook chunks often reference diagrams or apparatus setups via \`\\includegraphics{...}\` or \`Figure X\`.
   - If a question relies on an apparatus, circuit, or diagram, provide a clear, standalone **"Diagram Setup / Visual Description"** in text within the question so it is 100% self-contained without needing the image file.
3. **LaTeX Markup Cleansing:**
   - Ignore formatting tags (\`\\section\`, \`\\textbf\`, \`\\label\`, \`\\ref\`, \`\\cite\`, \`\\vspace\`, etc.). Extract only the core physical/chemical laws, definitions, proofs, data tables, and conceptual assertions.

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
  [Full question text formatted in clean Markdown with LaTeX math \`$ ... $\`]
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
Read \`{latex_chunk}\`, inspect \`{requested_selection}\`, and produce ONLY the requested questions following the exact template above.`;

// API Endpoints

// 1. Get sample topics
app.get('/api/samples', (req, res) => {
  res.json({ success: true, samples });
});

// 1b. Check API Status & Environment Key
app.get('/api/status', (req, res) => {
  res.json({
    success: true,
    hasEnvKey: !!process.env.GEMINI_API_KEY,
    model: process.env.GEMINI_MODEL || 'gemini-flash-lite-latest'
  });
});

// 1c. Chapter Orchestration Endpoints
app.get('/api/toc', (req, res) => {
  res.json({
    chapter: chapterData.chapter || 'Electromagnetic Induction',
    topics: (chapterData.toc && chapterData.toc.topics) || [
      "Faraday's Law of Induction",
      "Lenz's Law",
      "Eddy Currents"
    ]
  });
});

app.get('/api/chunks', (req, res) => {
  res.json(chapterData.chunks || []);
});

app.get('/api/questions', (req, res) => {
  res.json({
    chapter: chapterData.chapter || 'Electromagnetic Induction',
    questions: chapterData.questions || []
  });
});

// 1d. Prompt Template Endpoint
app.get('/api/prompt-template', (req, res) => {
  const userPromptTemplate = `INPUT PARAMETERS:
- {subject}: {subject}
- {grade}: {grade}
- {chapter_title}: {chapter_title}
- {topic_name}: {topic_name}
- {topic_weight}: {topic_weight}
- {allocated_quota}: {allocated_quota}
- {requested_selection}: {requested_selection}

LATEX CHUNK:
\`\`\`latex
{latex_chunk}
\`\`\`

Strictly obey all guidelines in the system context. Generate ONLY the requested questions following the exact output structure.`;

  res.json({
    success: true,
    systemPrompt: SYSTEM_PROMPT,
    userPromptTemplate,
    inputContract: [
      'subject',
      'grade',
      'chapter_title',
      'topic_name',
      'topic_weight',
      'allocated_quota',
      'latex_chunk',
      'requested_selection'
    ]
  });
});

// 2. Split large LaTeX into section/topic chunks
app.post('/api/split-chunk', (req, res) => {
  const { latex } = req.body;
  if (!latex || typeof latex !== 'string') {
    return res.status(400).json({ error: 'LaTeX content is required' });
  }

  // Regex to match \section{...} or \subsection{...}
  const sectionRegex = /\\(section|subsection|chapter)\*?\{([^}]+)\}/gi;
  const matches = [];
  let match;
  while ((match = sectionRegex.exec(latex)) !== null) {
    matches.push({
      type: match[1],
      title: match[2].trim(),
      startIndex: match.index
    });
  }

  if (matches.length === 0) {
    // If no sections found, treat the whole text as single topic chunk
    return res.json({
      success: true,
      topics: [
        {
          title: 'Full Chunk / Section',
          type: 'custom',
          content: latex.trim()
        }
      ]
    });
  }

  const topics = [];
  for (let i = 0; i < matches.length; i++) {
    const cur = matches[i];
    const nextStart = i + 1 < matches.length ? matches[i + 1].startIndex : latex.length;
    const chunkText = latex.substring(cur.startIndex, nextStart).trim();
    topics.push({
      title: cur.title,
      type: cur.type,
      content: chunkText
    });
  }

  res.json({ success: true, topics });
});

// 3. Verify Gemini API Key
app.post('/api/verify-key', async (req, res) => {
  const { apiKey } = req.body;
  if (!apiKey) {
    return res.status(400).json({ error: 'API key is required' });
  }

  const candidateModels = [
    process.env.GEMINI_MODEL,
    'gemini-flash-lite-latest',
    'gemini-3.5-flash-lite',
    'gemini-3.5-flash',
    'gemini-3.7-flash',
    'gemini-3.1-flash-lite',
    'gemini-flash-latest',
    'gemini-3.8-flash',
    'gemini-2.5-flash',
    'gemini-1.5-flash'
  ].filter(Boolean);

  let lastError = 'Invalid Gemini API key or quota exceeded';
  for (const modelName of candidateModels) {
    try {
      const url = `https://generativelanguage.googleapis.com/v1beta/models/${modelName}:generateContent?key=${apiKey}`;
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          contents: [{ parts: [{ text: 'Respond with OK.' }] }]
        })
      });

      if (response.ok) {
        return res.json({ success: true, model: modelName });
      } else {
        const errData = await response.json().catch(() => ({}));
        if (errData.error?.message) {
          lastError = errData.error.message;
        }
      }
    } catch (err) {
      lastError = err.message;
    }
  }

  res.status(400).json({ error: lastError });
});

// Dynamic taxonomy selector based on topic quota, difficulty, and weight
function buildRequestedSelection(allocatedQuota, difficultyMultiplier = 1.0, topicWeight = 0.0) {
  const quota = Number.isInteger(allocatedQuota) ? allocatedQuota : (parseInt(allocatedQuota, 10) || 0);
  if (quota <= 0) return '0 questions';
  const diff = (difficultyMultiplier !== undefined && difficultyMultiplier !== null && !isNaN(Number(difficultyMultiplier))) ? Number(difficultyMultiplier) : 1.0;
  const weight = (topicWeight !== undefined && topicWeight !== null && !isNaN(Number(topicWeight))) ? Number(topicWeight) : 0.0;
  const isHighRigor = diff >= 1.5 || weight >= 0.40;

  if (quota === 1) {
    return isHighRigor ? '1 Short Answer (3M)' : '1 Objective — MCQ (1M)';
  } else if (quota === 2) {
    return isHighRigor ? '1 Short Answer (3M), 1 Long Answer / Derivation (5M)' : '1 Objective — MCQ (1M), 1 Very Short Answer (2M)';
  } else if (quota === 3) {
    return isHighRigor ? '1 Objective — MCQ (1M), 1 Short Answer (3M), 1 Long Answer / Derivation (5M)' : '1 Objective — MCQ (1M), 1 Assertion-Reason (1M), 1 Very Short Answer (2M)';
  } else if (quota === 4) {
    return isHighRigor ? '1 Objective — MCQ (1M), 1 Short Answer (3M), 1 Case-Based (4M), 1 Long Answer / Derivation (5M)' : '1 Objective — MCQ (1M), 1 Assertion-Reason (1M), 1 Very Short Answer (2M), 1 Short Answer (3M)';
  } else if (quota === 5) {
    return isHighRigor ? '1 Objective — MCQ (1M), 1 Assertion-Reason (1M), 1 Short Answer (3M), 1 Case-Based (4M), 1 Long Answer / Derivation (5M)' : '2 Objective — MCQs (1M), 1 Assertion-Reason (1M), 1 Very Short Answer (2M), 1 Short Answer (3M)';
  } else if (quota === 6) {
    return isHighRigor ? '2 Objective — MCQs (1M), 1 Assertion-Reason (1M), 1 Short Answer (3M), 1 Case-Based (4M), 1 Long Answer / Derivation (5M)' : '2 Objective — MCQs (1M), 1 Assertion-Reason (1M), 2 Very Short Answers (2M), 1 Short Answer (3M)';
  } else {
    if (isHighRigor) {
      const nLa = Math.max(1, Math.floor(quota / 5));
      const nCase = Math.max(1, Math.floor(quota / 6));
      const nSa = Math.max(1, Math.floor(quota / 4));
      const nVsa = Math.max(1, Math.floor(quota / 6));
      const nAr = 1;
      const nMcq = Math.max(1, quota - (nLa + nCase + nSa + nVsa + nAr));
      return `${nMcq} Objective — MCQ${nMcq > 1 ? 's' : ''} (1M), ${nAr} Assertion-Reason (1M), ${nVsa} Very Short Answer${nVsa > 1 ? 's' : ''} (2M), ${nSa} Short Answer${nSa > 1 ? 's' : ''} (3M), ${nCase} Case-Based (4M), ${nLa} Long Answer / Derivation${nLa > 1 ? 's' : ''} (5M)`;
    } else {
      const nMcq = Math.max(2, Math.floor(quota / 3));
      const nAr = Math.max(1, Math.floor(quota / 6));
      const nVsa = Math.max(2, Math.floor(quota / 4));
      const rem = quota - (nMcq + nAr + nVsa);
      const nSa = Math.max(1, rem > 0 ? rem : 1);
      return `${nMcq} Objective — MCQ${nMcq > 1 ? 's' : ''} (1M), ${nAr} Assertion-Reason (1M), ${nVsa} Very Short Answer${nVsa > 1 ? 's' : ''} (2M), ${nSa} Short Answer${nSa > 1 ? 's' : ''} (3M)`;
    }
  }
}

// Selection parser for CBSE question categories and counts
function parseSelectionCounts(selection, allocatedQuota, difficultyMultiplier, topicWeight) {
  let sel = selection || '';
  if (!sel || ['auto', 'dynamic', 'none'].includes(sel.trim().toLowerCase()) || sel.includes('Select at least one')) {
    sel = buildRequestedSelection(allocatedQuota || 3, difficultyMultiplier || 1.0, topicWeight || 0.0);
  }
  const parts = sel.split(',').map(s => s.trim()).filter(Boolean);
  const categoryCounts = [];

  for (const part of parts) {
    const m = part.match(/^\s*(\d+)\s*(.*)/);
    const count = m ? parseInt(m[1], 10) : 1;
    const desc = m ? m[2].toLowerCase() : part.toLowerCase();

    let cat = null;
    if (desc.includes('assertion') || desc.includes('a-r')) {
      cat = 'ar';
    } else if (desc.includes('very short') || desc.includes('vsa') || desc.includes('2m') || desc.includes('2-mark')) {
      cat = 'vsa';
    } else if (/(?<!very\s)short answer|\b(?<!v)sa\b|\b3m\b|\b3-mark\b/i.test(desc)) {
      cat = 'sa';
    } else if (desc.includes('case') || desc.includes('4m') || desc.includes('4-mark')) {
      cat = 'case';
    } else if (desc.includes('long') || desc.includes('derivation') || desc.includes('5m') || desc.includes('5-mark')) {
      cat = 'la';
    } else if (desc.includes('mcq') || desc.includes('objective') || desc.includes('1m') || desc.includes('1-mark')) {
      cat = 'mcq';
    }

    if (cat) {
      categoryCounts.push({ category: cat, count });
    }
  }

  if (categoryCounts.length === 0) {
    categoryCounts.push({ category: 'mcq', count: allocatedQuota || 1 });
  }

  if (allocatedQuota !== undefined && allocatedQuota !== null && allocatedQuota > 0) {
    const currentTotal = categoryCounts.reduce((acc, c) => acc + c.count, 0);
    if (currentTotal !== allocatedQuota) {
      const diff = allocatedQuota - currentTotal;
      if (diff > 0) {
        categoryCounts[0].count += diff;
      } else {
        const newCounts = [];
        let remaining = allocatedQuota;
        for (const item of categoryCounts) {
          const take = Math.min(item.count, remaining);
          if (take > 0) {
            newCounts.push({ category: item.category, count: take });
            remaining -= take;
          }
        }
        return newCounts;
      }
    }
  }

  return categoryCounts;
}

function buildMockQuestionItem(subject, grade, chapter, topic, latexChunk, category, index, qNum) {
  const tLower = (topic || '').toLowerCase();
  const cLower = (chapter || '').toLowerCase();
  const chunkLower = (latexChunk || '').toLowerCase();
  const sLower = (subject || '').toLowerCase();

  const isFaraday = tLower.includes('faraday') || chunkLower.includes('faraday') || chunkLower.includes('\\mathcal{e}');
  const isLenz = tLower.includes('lenz') || chunkLower.includes('lenz');
  const isEddy = tLower.includes('eddy') || chunkLower.includes('damping') || chunkLower.includes('foucault');
  const isPhysics = sLower.includes('physic') || tLower.includes('prism') || isFaraday || isLenz || isEddy;
  const isChem = sLower.includes('chem') || tLower.includes('nernst');
  const isMath = sLower.includes('math') || tLower.includes('parts');

  if (category === 'mcq') {
    if (isFaraday) {
      if (index === 0) {
        return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Faraday's Law and magnitude of induced electromotive force
- **Question:**
  A circular loop of wire of radius $r = 0.1\\text{ m}$ is placed perpendicular to a uniform magnetic field. If the magnetic field decreases uniformly from $1.5\\text{ T}$ to $0.5\\text{ T}$ in $0.2\\text{ s}$, the magnitude of the EMF induced in the loop is:
  (A) $0.157\\text{ V}$
  (B) $0.314\\text{ V}$
  (C) $0.050\\text{ V}$
  (D) $0.628\\text{ V}$

- **Marking Scheme & Solution:**
  - Area $A = \\pi r^2 = \\pi (0.1)^2 = 0.01\\pi\\text{ m}^2$. Rate of change $\\frac{\\Delta B}{\\Delta t} = \\frac{1.5 - 0.5}{0.2} = 5.0\\text{ T/s}$. — **[1/2 M]**
  - Induced EMF $\\mathcal{E} = A\\frac{\\Delta B}{\\Delta t} = 0.01\\pi \\times 5.0 = 0.05\\pi \\approx 0.157\\text{ V}$. Correct option is (A). — **[1/2 M]**

- **Why this Tier:** Core quantitative staple testing standard application of Faraday's Law of induction $\\mathcal{E} = -d\\Phi/dt$.
---`;
      } else {
        const turns = 50 + index * 10;
        return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Magnetic flux linkage and induced EMF in rotating coil
- **Question:**
  A coil of ${turns} turns and area $0.02\\text{ m}^2$ is rotated through $90^\\circ$ from perpendicular to parallel in a uniform magnetic field $B = 0.4\\text{ T}$ in $0.1\\text{ s}$. The average induced EMF is:
  (A) ${(turns * 0.4 * 0.02 / 0.2).toFixed(1)}\\text{ V}$
  (B) ${(turns * 0.4 * 0.02 / 0.1).toFixed(1)}\\text{ V}$
  (C) ${(turns * 0.4 * 0.02 / 0.05).toFixed(1)}\\text{ V}$
  (D) $0.4\\text{ V}$

- **Marking Scheme & Solution:**
  - Initial flux $\\Phi_1 = N B A = ${turns} \\times 0.4 \\times 0.02\\text{ Wb}$, final flux $\\Phi_2 = 0$. — **[1/2 M]**
  - Average induced EMF $|\\mathcal{E}| = \\frac{\\Delta\\Phi}{\\Delta t} = ${(turns * 0.4 * 0.02 / 0.1).toFixed(1)}\\text{ V}$. Correct option is (B). — **[1/2 M]**

- **Why this Tier:** Core quantitative test of magnetic orientation change and Faraday's flux linkage.
---`;
      }
    } else if (isLenz) {
      if (index === 0) {
        return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
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
---`;
      } else {
        return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
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
---`;
      }
    } else if (isEddy) {
      if (index === 0) {
        return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
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
---`;
      } else {
        return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
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
---`;
      }
    } else if (isPhysics) {
      return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Condition for minimum deviation in a triangular prism
- **Question:**
  A light ray passes through an equilateral triangular glass prism ($A = 60^\\circ$) at minimum deviation. If the angle of incidence is $48^\\circ$, the angle of emergence $e$ and the angle of refraction $r_1$ at the first face are respectively:
  (A) $42^\\circ$ and $30^\\circ$
  (B) $48^\\circ$ and $30^\\circ$
  (C) $48^\\circ$ and $24^\\circ$
  (D) $30^\\circ$ and $48^\\circ$

- **Marking Scheme & Solution:**
  - At minimum deviation condition, $i = e$, hence $e = 48^\\circ$. Also $r_1 = A/2 = 30^\\circ$. Correct option is (B). — **[1 M]**

- **Why this Tier:** Fundamental board staple testing the symmetry condition ($i=e, r_1=r_2=A/2$) at minimum deviation.
---`;
    } else if (isChem) {
      return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Dependence of electrode potential on ion concentration (Nernst equation)
- **Question:**
  For the half-cell reaction $\\text{Zn}^{2+}(aq) + 2e^- \\rightarrow \\text{Zn}(s)$, if the concentration of $\\text{Zn}^{2+}$ is decreased from $1.0\\text{ M}$ to $0.01\\text{ M}$ at $298\\text{ K}$, the electrode potential $E_{(\\text{Zn}^{2+}/\\text{Zn})}$:
  (A) Increases by $0.0591\\text{ V}$
  (B) Decreases by $0.0591\\text{ V}$
  (C) Decreases by $0.1182\\text{ V}$
  (D) Remains unchanged

- **Marking Scheme & Solution:**
  - $\\Delta E = -\\frac{0.0591}{2}\\log_{10}(10^2) = -0.0591\\text{ V}$. Correct option is (B). — **[1 M]**

- **Why this Tier:** Core quantitative application of Nernst equation for single electrodes frequently tested in Section A.
---`;
    } else {
      return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Choice of first function using ILATE rule
- **Question:**
  While evaluating $\\int x^2 \\sin^{-1} x \\, dx$ using the method of integration by parts, the first function $u(x)$ according to the ILATE priority rule must be:
  (A) $x^2$
  (B) $\\sin^{-1} x$
  (C) $2x$
  (D) $\\frac{1}{\\sqrt{1-x^2}}$

- **Marking Scheme & Solution:**
  - In ILATE rule, 'I' (Inverse Trigonometric) takes precedence over 'A' (Algebraic). Correct option is (B). — **[1 M]**

- **Why this Tier:** Direct test of standard calculus conventions tested in Section A of CBSE Class 12 Mathematics.
---`;
    }
  }

  if (category === 'ar') {
    if (isLenz || isFaraday) {
      return `### Q${qNum} | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
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
  - Induced magnetic polarity opposes approaching magnet, creating repulsive force. Hence Assertion is True. — **[1/2 M]**
  - Reason provides the exact thermodynamic foundation (conservation of energy). Correct answer is (a). — **[1/2 M]**

- **Why this Tier:** Evaluates understanding of energy conservation in electromagnetic phenomena.
---`;
    } else if (isEddy) {
      return `### Q${qNum} | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Lamination of magnetic cores and eddy current damping
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** In a dead-beat galvanometer, the coil former is deliberately made of a non-conducting plastic material.
  **Reason (R):** Eddy currents induced in a metallic former provide electromagnetic damping that brings the pointer rapidly to rest.

- **Marking Scheme & Solution:**
  - Assertion is false because former is made of metallic conductor (copper/aluminum) to facilitate eddy currents. — **[1/2 M]**
  - Reason is true. Hence code (d) A is false but R is true. — **[1/2 M]**

- **Why this Tier:** Conceptual differentiator identifying misconceptions regarding damping mechanism in electrical instruments.
---`;
    } else {
      return `### Q${qNum} | Assertion-Reason (A-R) | 1 Mark(s) | 🟡 Tier 2
- **Target Concept:** Thermodynamic spontaneity and Cell Potential relation
- **Question:**
  Select the correct code:
  (a) Both A and R are true and R is the correct explanation of A.
  (b) Both A and R are true but R is NOT the correct explanation of A.
  (c) A is true but R is false.
  (d) A is false but R is true.

  **Assertion (A):** When a galvanic cell reaches chemical equilibrium, both $E_{cell}$ and $E^\\circ_{cell}$ become zero.
  **Reason (R):** At equilibrium, the reaction quotient $Q$ equals $K_c$, and no net current flows through the external circuit.

- **Marking Scheme & Solution:**
  - Assertion is false because only $E_{cell} = 0$ at equilibrium; standard potential $E^\\circ_{cell}$ is a constant. Reason is true. Correct code is (d). — **[1 M]**

- **Why this Tier:** Recurring CBSE trap testing the distinction between instantaneous potential and standard potential.
---`;
    }
  }

  if (category === 'vsa') {
    if (isFaraday) {
      return `### Q${qNum} | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Magnetic flux linkage and induced EMF formula
- **Question:**
  State Faraday's law of electromagnetic induction. Express the magnitude of induced EMF mathematically for a coil containing $N$ closely wound turns.

- **Marking Scheme & Solution:**
  - Statement: The magnitude of the induced electromotive force in a circuit is directly proportional to the time rate of change of magnetic flux through the circuit. — **[1 M]**
  - Formula: $\\mathcal{E} = -N \\frac{d\\Phi_B}{dt}$ where $N$ is turn count and $\\Phi_B$ is flux. — **[1 M]**

- **Why this Tier:** Standard 2-mark law definition and formula requirement in Section B.
---`;
    } else if (isLenz) {
      return `### Q${qNum} | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Statement and physical significance of Lenz's Law
- **Question:**
  State Lenz's law. Why does a metallic ring feel warm when a bar magnet is repeatedly thrust into and pulled out of it?

- **Marking Scheme & Solution:**
  - Statement: The polarity of the induced EMF produces a current which opposes the change in magnetic flux that produces it. — **[1 M]**
  - Continuous movement requires external mechanical work against magnetic opposition, which is converted to electrical energy and dissipated as Joule heat ($I^2 R t$). — **[1 M]**

- **Why this Tier:** Essential conceptual explanation directly referenced in NCERT questions.
---`;
    } else if (isEddy) {
      return `### Q${qNum} | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Nature and mitigation of Eddy Currents
- **Question:**
  What are eddy currents? How are eddy current losses minimized in the magnetic cores of transformers?

- **Marking Scheme & Solution:**
  - Definition: Induced swirling loops of electric current set up in bulk conductors subjected to changing magnetic flux. — **[1 M]**
  - Mitigation: Using laminated magnetic cores insulated by lacquer coatings parallel to magnetic lines of force. — **[1 M]**

- **Why this Tier:** Core syllabus foundation testing physical definition and practical engineering remedy.
---`;
    } else {
      return `### Q${qNum} | Very Short Answer (VSA) | 2 Mark(s) | 🔴 Tier 1
- **Target Concept:** Standard Gibbs energy calculation from standard cell potential
- **Question:**
  The standard EMF of a Daniell cell is $1.10\\text{ V}$ at $298\\text{ K}$. Calculate the standard Gibbs energy change $\\Delta_r G^\\circ$ for the cell reaction. ($F = 96500\\text{ C mol}^{-1}$).

- **Marking Scheme & Solution:**
  - Formula: $\\Delta_r G^\\circ = -nFE^\\circ_{cell}$ with $n = 2$. — **[1/2 M]**
  - Substitution: $\\Delta_r G^\\circ = -2 \\times 96500 \\times 1.10 = -212.3\\text{ kJ mol}^{-1}$. — **[1 1/2 M]**

- **Why this Tier:** Standard 2-mark formula substitution question guaranteed to feature in board exams.
---`;
    }
  }

  if (category === 'sa') {
    if (isFaraday) {
      return `### Q${qNum} | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Induced EMF calculation in a circular coil
- **Question:**
  A circular coil of 200 turns and radius $10\\text{ cm}$ is placed perpendicular to a uniform magnetic field changing at a constant rate of $0.5\\text{ T/s}$.
  (a) Calculate the magnetic flux linked with each turn when the magnetic field is $0.8\\text{ T}$.
  (b) Determine the magnitude of the induced EMF in the coil.
  (c) If the electrical resistance of the coil is $4\\,\\Omega$, find the induced current.

- **Marking Scheme & Solution:**
  - (a) Area $A = \\pi (0.1)^2 = 0.01\\pi\\text{ m}^2$. $\\Phi_B = B A = 0.8 \\times 0.01\\pi = 0.0251\\text{ Wb}$. — **[1 M]**
  - (b) Induced EMF $|\\mathcal{E}| = N A \\frac{dB}{dt} = 200 \\times 0.01\\pi \\times 0.5 = \\pi \\approx 3.14\\text{ V}$. — **[1 M]**
  - (c) Induced current $I = \\frac{\\mathcal{E}}{R} = \\frac{3.14\\text{ V}}{4\\,\\Omega} = 0.785\\text{ A}$. — **[1 M]**

- **Why this Tier:** Recurring 3-mark multi-step numerical in CBSE Physics Section C.
---`;
    } else if (isEddy) {
      return `### Q${qNum} | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
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
---`;
    } else {
      return `### Q${qNum} | Short Answer (SA) | 3 Mark(s) | 🔴 Tier 1
- **Target Concept:** Evaluation of integrals using $\\int e^x [f(x) + f'(x)] dx$
- **Question:**
  Evaluate the integral:
  $$\\int e^x \\left( \\frac{1 + \\sin x}{1 + \\cos x} \\right) dx$$

- **Marking Scheme & Solution:**
  - Expressing integrand: $\\frac{1+\\sin x}{1+\\cos x} = \\frac{1}{2}\\sec^2(x/2) + \\tan(x/2)$. — **[1 M]**
  - Identifying standard form: Let $f(x) = \\tan(x/2)$, then $f'(x) = \\frac{1}{2}\\sec^2(x/2)$. — **[1 M]**
  - Applying theorem: $\\int e^x [f(x) + f'(x)] dx = e^x \\tan(x/2) + C$. — **[1 M]**

- **Why this Tier:** Benchmark 3-mark calculus question testing recognition of the exponential derivative identity.
---`;
    }
  }

  if (category === 'case') {
    return `### Q${qNum} | Case-Based / Source-Based | 4 Mark(s) | 🟡 Tier 2
- **Target Concept:** Electromagnetic Induction in industrial braking and power conversion
- **Question:**
  **Case Study Passage:**
  Michael Faraday's discovery of electromagnetic induction established that any change in magnetic flux through a circuit induces an electromotive force. Soon after, Heinrich Lenz demonstrated that the polarity of this induced EMF invariably opposes the change producing it, maintaining strict adherence to the law of conservation of energy. In bulk conductors, these induced currents form closed swirling loops called eddy currents. While eddy currents cause significant power dissipation in transformer cores, they are cleverly exploited in dead-beat moving coil galvanometers and frictionless electromagnetic brakes for high-speed bullet trains.

  Answer the following questions:
  (a) Write the mathematical relationship between induced EMF $\\mathcal{E}$ and magnetic flux $\\Phi_B$ for a coil of $N$ turns. **(1 Mark)**
  (b) Why does a metal disc dropped between the pole pieces of an electromagnet decelerate rapidly when current is turned on? **(1 Mark)**
  (c) A circular coil of area $0.05\\text{ m}^2$ with 100 turns rotates in a uniform field of $0.2\\text{ T}$ at $50\\text{ rev/s}$. Calculate the peak EMF generated. **(2 Marks)**
  **OR**
  (c) Explain how lamination of a soft iron core minimizes eddy current heat dissipation, stating the quantitative dependence on lamination thickness $t$. **(2 Marks)**

- **Marking Scheme & Solution:**
  - (a) $\\mathcal{E} = -N \\frac{d\\Phi_B}{dt}$. — **[1 M]**
  - (b) Eddy currents induced in the moving disc create opposing magnetic force (Lenz's Law), dissipating kinetic energy as heat. — **[1 M]**
  - (c) $\\mathcal{E}_0 = N B A \\omega = 100 \\times 0.2 \\times 0.05 \\times (2\\pi \\times 50) = 314.16\\text{ V}$. — **[2 M]**
  - **OR** (c) Lamination creates insulating boundaries reducing eddy loop paths; power loss is proportional to $t^2$. — **[2 M]**

- **Why this Tier:** Authentic CBSE Section E competency-based assessment evaluating real-world application of induction.
---`;
  }

  if (category === 'la') {
    return `### Q${qNum} | Long Answer (LA) / Derivation | 5 Mark(s) | 🔴 Tier 1
- **Target Concept:** First-principles derivation of alternating EMF induced in a rotating coil
- **Question:**
  (a) A rectangular coil of $N$ turns, each of area $A$, rotates with a constant angular velocity $\\omega$ about an axis perpendicular to a uniform magnetic field $\\mathbf{B}$. Derive the expression for the instantaneous electromotive force:
  $$\\mathcal{E} = N B A \\omega \\sin(\\omega t)$$
  
  (b) Diagram Setup: Rectangular coil $PQRS$ of dimensions $l \\times b$ rotating about axis $OO'$ perpendicular to uniform magnetic field lines $\\mathbf{B}$.
  
  (c) State the position of the coil relative to the magnetic field lines when the induced EMF is (i) zero, and (ii) maximum.

- **Marking Scheme & Solution:**
  - (a) Magnetic flux at time $t$: $\\Phi_B = \\mathbf{B} \\cdot \\mathbf{A} = B A \\cos(\\omega t)$. — **[1 M]**
  - Applying Faraday's law: $\\mathcal{E} = -N \\frac{d\\Phi_B}{dt} = -N \\frac{d}{dt}[B A \\cos(\\omega t)]$. — **[1 M]**
  - Differentiation step: $\\frac{d}{dt}[\\cos(\\omega t)] = -\\omega \\sin(\\omega t) \\implies \\mathcal{E} = N B A \\omega \\sin(\\omega t)$. — **[1 M]**
  - (b) Neat schematic showing rotating coil axis and angle $\\theta = \\omega t$. — **[1 M]**
  - (c) (i) $\\mathcal{E} = 0$ when coil plane is perpendicular to field ($\\omega t = 0^\\circ$ or $180^\\circ$). (ii) $\\mathcal{E} = \\mathcal{E}_{max}$ when coil plane is parallel to field ($\\omega t = 90^\\circ$ or $270^\\circ$). — **[1 M]**

- **Why this Tier:** Canonical 5-mark board derivation featuring in CBSE Class 12 Physics Section D.
---`;
  }

  return `### Q${qNum} | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
- **Target Concept:** Core concept from topic chunk: ${topic}
- **Question:**
  Based on the topic ${topic}, identify the correct fundamental principle governing this system.
  (A) Option A
  (B) Option B
  (C) Option C
  (D) Option D

- **Marking Scheme & Solution:**
  - Correct identification. — **[1 M]**

- **Why this Tier:** Core syllabus foundation.
---`;
}

// Mock response generator for demo mode or pre-loaded chunks
function generateMockResponse(subject, grade, chapter, topic, selection, latexChunk, topicWeight, allocatedQuota, startQIndex = 1, difficultyMultiplier = 1.0) {
  if (allocatedQuota !== undefined && allocatedQuota !== null && allocatedQuota <= 0) {
    return '';
  }

  let effectiveSelection = selection;
  if (!effectiveSelection || ['auto', 'dynamic', 'none'].includes(String(effectiveSelection).trim().toLowerCase()) || String(effectiveSelection).includes('Select at least one')) {
    const qCount = allocatedQuota || 3;
    effectiveSelection = buildRequestedSelection(qCount, difficultyMultiplier || 1.0, topicWeight || 0.0);
  }

  const categoryCounts = parseSelectionCounts(effectiveSelection, allocatedQuota, difficultyMultiplier, topicWeight);
  const outputs = [];
  let currentQ = startQIndex;

  for (const item of categoryCounts) {
    for (let idx = 0; idx < item.count; idx++) {
      const qText = buildMockQuestionItem(
        subject,
        grade,
        chapter,
        topic,
        latexChunk,
        item.category,
        idx,
        currentQ
      );
      outputs.push(qText);
      currentQ++;
    }
  }

  return outputs.join('\n\n').trim();
}

// 4. Generate Questions Endpoint
app.post('/api/generate', async (req, res) => {
  const {
    subject,
    grade,
    chapter_title,
    topic_name,
    topic_weight,
    allocated_quota,
    latex_chunk,
    requested_selection,
    apiKey,
    useMock,
    target_questions,
    dynamic_target,
    validate_range,
    strict,
  } = req.body;

  if (!latex_chunk) {
    return res.status(400).json({ error: 'latex_chunk is required' });
  }

  let resolvedTarget = target_questions;
  let targetMode = undefined;
  if (dynamic_target === true || (typeof target_questions === 'string' && ['auto', 'dynamic'].includes(target_questions.trim().toLowerCase()))) {
    targetMode = 'dynamic';
    resolvedTarget = 'auto';
  } else if (target_questions !== undefined && target_questions !== null && target_questions !== '') {
    const num = parseInt(target_questions, 10);
    if (!isNaN(num)) {
      if (num < MIN_MANUAL_QUESTIONS || num > MAX_MANUAL_QUESTIONS) {
        if (strict) {
          return res.status(400).json({ error: `Manual target questions must be between ${MIN_MANUAL_QUESTIONS} and ${MAX_MANUAL_QUESTIONS} (inclusive), got ${num}.` });
        }
        if (validate_range) {
          resolvedTarget = Math.max(MIN_MANUAL_QUESTIONS, Math.min(MAX_MANUAL_QUESTIONS, num));
        } else {
          resolvedTarget = num;
        }
      } else {
        resolvedTarget = num;
      }
      targetMode = 'manual';
    }
  }

  // Resolve requested_selection: dynamic auto-allocation if omitted or 'auto'
  let resolvedSelection = requested_selection;
  let selectionMode = 'manual';
  if (!resolvedSelection || ['auto', 'dynamic', 'none', 'default'].includes(String(resolvedSelection).trim().toLowerCase()) || String(resolvedSelection).includes('Select at least one')) {
    selectionMode = 'dynamic';
    let quota = 3;
    if (allocated_quota !== undefined && allocated_quota !== null && !isNaN(Number(allocated_quota))) {
      quota = Number(allocated_quota);
    } else if (typeof resolvedTarget === 'number' && resolvedTarget > 0) {
      quota = resolvedTarget;
    }
    const diff = (req.body.difficulty_multiplier !== undefined && req.body.difficulty_multiplier !== null)
      ? Number(req.body.difficulty_multiplier)
      : (req.body.difficulty !== undefined ? Number(req.body.difficulty) : 1.0);
    const weight = (topic_weight !== undefined && topic_weight !== null && !isNaN(Number(topic_weight)))
      ? Number(topic_weight)
      : 0.33;
    resolvedSelection = buildRequestedSelection(quota, diff, weight);
  }

  const effectiveApiKey = apiKey || process.env.GEMINI_API_KEY;

  let userPrompt = `INPUT PARAMETERS:
- {subject}: ${subject || 'CBSE Subject'}
- {grade}: ${grade || '12'}
- {chapter_title}: ${chapter_title || 'CBSE Chapter'}
- {topic_name}: ${topic_name || 'CBSE Topic'}`;

  if (topic_weight !== undefined && topic_weight !== null) {
    userPrompt += `\n- {topic_weight}: ${topic_weight}`;
  }
  if (allocated_quota !== undefined && allocated_quota !== null) {
    userPrompt += `\n- {allocated_quota}: ${allocated_quota}`;
  }
  if (resolvedTarget !== undefined && resolvedTarget !== null) {
    userPrompt += `\n- {target_questions}: ${resolvedTarget}`;
  }
  if (targetMode) {
    userPrompt += `\n- {target_questions_mode}: ${targetMode}`;
  }
  userPrompt += `\n- {requested_selection}: ${resolvedSelection}

LATEX CHUNK:
\`\`\`latex
${latex_chunk}
\`\`\`

Strictly obey all guidelines in the system context. Generate ONLY the requested questions following the exact output structure.`;

  // If mock mode is requested or no API key is available
  if (useMock || !effectiveApiKey) {
    const mockOutput = generateMockResponse(
      subject || 'General Science',
      grade || '12',
      chapter_title || 'General Chapter',
      topic_name || 'General Topic',
      resolvedSelection,
      latex_chunk,
      topic_weight,
      allocated_quota,
      1,
      req.body.difficulty_multiplier || req.body.difficulty || 1.0
    );
    return res.json({
      success: true,
      mode: 'mock',
      output: mockOutput,
      target_questions: resolvedTarget,
      target_questions_mode: targetMode,
      requested_selection: resolvedSelection,
      selection_mode: selectionMode,
      promptUsed: {
        systemPrompt: SYSTEM_PROMPT,
        userPrompt
      }
    });
  }

  // Real LLM call to Gemini Flash
  try {
    const requestBody = {
      system_instruction: {
        parts: [{ text: SYSTEM_PROMPT }]
      },
      contents: [
        {
          role: 'user',
          parts: [{ text: userPrompt }]
        }
      ],
      generationConfig: {
        temperature: 0.2,
        topP: 0.95
      }
    };

    const candidateModels = [
      process.env.GEMINI_MODEL,
      'gemini-flash-lite-latest',
      'gemini-3.5-flash-lite',
      'gemini-3.5-flash',
      'gemini-3.7-flash',
      'gemini-3.1-flash-lite',
      'gemini-flash-latest',
      'gemini-3.8-flash',
      'gemini-2.5-flash',
      'gemini-1.5-flash'
    ].filter(Boolean);
    let geminiRes = null;
    let selectedModel = '';
    for (const modelName of candidateModels) {
      const url = `https://generativelanguage.googleapis.com/v1beta/models/${modelName}:generateContent?key=${effectiveApiKey}`;
      try {
        geminiRes = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(requestBody)
        });
        if (geminiRes.ok) {
          selectedModel = modelName;
          break;
        }
      } catch (err) {
        console.warn(`Request failed for model ${modelName}:`, err.message);
      }
    }

    if (!geminiRes || !geminiRes.ok) {
      const errBody = await (geminiRes ? geminiRes.json().catch(() => ({})) : {});
      return res.status((geminiRes && geminiRes.status) || 500).json({
        error: errBody.error?.message || 'Failed to generate questions with Gemini Flash'
      });
    }

    const data = await geminiRes.json();
    const generatedText = data.candidates?.[0]?.content?.parts?.[0]?.text || '';

    res.json({
      success: true,
      mode: 'live',
      model: selectedModel,
      output: generatedText,
      target_questions: resolvedTarget,
      target_questions_mode: targetMode,
      requested_selection: resolvedSelection,
      selection_mode: selectionMode,
      promptUsed: {
        systemPrompt: SYSTEM_PROMPT,
        userPrompt
      }
    });
  } catch (err) {
    console.error('Generation error:', err);
    res.status(500).json({ error: err.message });
  }
});

const MIN_MANUAL_QUESTIONS = 10;
const MAX_MANUAL_QUESTIONS = 50;

// Dynamic Question Budgeting Algorithm
function safeNumber(val, defaultVal = 0.0) {
  if (val === undefined || val === null) return defaultVal;
  const num = Number(val);
  return Number.isNaN(num) || !Number.isFinite(num) ? defaultVal : num;
}

// Dynamic Question Budgeting Algorithm
function computeDynamicTargetQuestions(topicsOrManifest, options = {}) {
  let topics = [];
  if (Array.isArray(topicsOrManifest)) {
    topics = topicsOrManifest;
  } else if (topicsOrManifest && Array.isArray(topicsOrManifest.topics)) {
    topics = topicsOrManifest.topics;
  }
  if (!topics || topics.length === 0) {
    return 0;
  }

  const N = topics.length;
  const prior = safeNumber(options.prior, 0.5);
  const c1 = safeNumber(options.c1, 0.50);
  const c2 = safeNumber(options.c2, 0.50);

  let totalRawW = 0.0;
  let weightedDiffSum = 0.0;
  const rawWList = [];

  for (const t of topics) {
    if (!t || typeof t !== 'object') continue;
    const impact = Math.max(0.0, safeNumber(t.question_impact_score !== undefined ? t.question_impact_score : t.existing_questions, 0.0));
    const diff = Math.max(0.1, safeNumber(t.difficulty_multiplier, 1.0));
    const rawW = (impact + prior) * diff;
    rawWList.push({ topic: t, impact, diff, rawW });
    totalRawW += rawW;
    weightedDiffSum += rawW * diff;
  }

  const minQ = options.min_questions !== undefined
    ? Math.max(0, Math.round(safeNumber(options.min_questions, 5)))
    : Math.min(5, Math.max(1, N));
  const maxQ = Math.max(minQ, options.max_questions !== undefined
    ? Math.max(minQ, Math.round(safeNumber(options.max_questions, 50)))
    : 50);

  if (totalRawW <= 0) {
    return minQ;
  }

  const weightedDiff = weightedDiffSum / totalRawW;
  const diffDensity = Math.max(0.1, weightedDiff / 1.5);

  let sigCount = 0;
  for (const item of rawWList) {
    const relShare = item.rawW / totalRawW;
    if (item.impact > 0.0 || relShare >= (0.5 / N)) {
      sigCount++;
    }
  }

  if (sigCount === 0) {
    sigCount = Math.max(1, N);
  }

  const rawTarget = (c1 * sigCount + c2 * Math.sqrt(totalRawW)) * diffDensity;
  return Math.max(minQ, Math.min(maxQ, Math.round(rawTarget)));
}

function apportionQuotas(topics, targetQuestions, options = {}) {
  if (!topics || !Array.isArray(topics) || topics.length === 0) return [];
  const targetQ = Math.max(0, parseInt(targetQuestions, 10) || 0);
  if (targetQ <= 0) {
    return topics.map(t => (typeof t === 'object' && t !== null ? { ...t, allocated_quota: 0 } : t));
  }

  const prior = safeNumber(options.prior, 0.5);
  const rawWeights = topics.map(t => {
    if (!t || typeof t !== 'object') return 0.0;
    const impact = Math.max(0.0, safeNumber(t.question_impact_score !== undefined ? t.question_impact_score : t.existing_questions, 0.0));
    const diff = Math.max(0.1, safeNumber(t.difficulty_multiplier, 1.0));
    return (impact + prior) * diff;
  });

  const totalW = rawWeights.reduce((a, b) => a + b, 0);
  if (totalW > 0) {
    const exactShares = rawWeights.map(w => (w / totalW) * targetQ);
    const floorShares = exactShares.map(s => Math.floor(s));
    let remainder = targetQ - floorShares.reduce((a, b) => a + b, 0);

    const indexed = topics.map((t, i) => ({
      index: i,
      fraction: exactShares[i] - floorShares[i],
      rawWeight: rawWeights[i]
    }));

    indexed.sort((a, b) => {
      if (b.fraction !== a.fraction) return b.fraction - a.fraction;
      return b.rawWeight - a.rawWeight;
    });

    for (let r = 0; r < remainder; r++) {
      floorShares[indexed[r % indexed.length].index] += 1;
    }

    return topics.map((t, i) => ({
      ...(typeof t === 'object' && t !== null ? t : { topic_name: String(t) }),
      allocated_quota: floorShares[i]
    }));
  } else {
    const base = Math.floor(targetQ / topics.length);
    const rem = targetQ % topics.length;
    return topics.map((t, i) => ({
      ...(typeof t === 'object' && t !== null ? t : { topic_name: String(t) }),
      allocated_quota: base + (i < rem ? 1 : 0)
    }));
  }
}

// 5. Generate Weighted Exam Across Manifest
app.post('/api/generate-weighted', async (req, res) => {
  let {
    manifest,
    chunks,
    subject = 'Physics',
    grade = '12',
    chapter_title = 'Electromagnetic Induction',
    apiKey,
    useMock = true,
    dynamic_target,
    target_questions,
    min_questions,
    max_questions,
    c1,
    c2,
    prior
  } = req.body;

  if (!manifest || !Array.isArray(manifest.topics)) {
    return res.status(400).json({ error: 'Valid manifest with topics is required' });
  }

  const rawTarget = (target_questions !== undefined && target_questions !== null && target_questions !== '')
    ? target_questions
    : (manifest && manifest.target_questions !== undefined && manifest.target_questions !== null && manifest.target_questions !== '' ? manifest.target_questions : undefined);

  const isExplicitDynamic = dynamic_target === true ||
    (typeof dynamic_target === 'string' && dynamic_target.trim().toLowerCase() === 'true') ||
    (typeof rawTarget === 'string' && ['auto', 'dynamic'].includes(rawTarget.trim().toLowerCase())) ||
    (manifest && manifest.is_dynamic_target === true && rawTarget === undefined);

  const hasManualTarget = !isExplicitDynamic && rawTarget !== undefined;

  const budgetOptions = {
    min_questions: min_questions !== undefined ? min_questions : (manifest && manifest.min_questions),
    max_questions: max_questions !== undefined ? max_questions : (manifest && manifest.max_questions),
    c1: c1 !== undefined ? c1 : (manifest && manifest.c1),
    c2: c2 !== undefined ? c2 : (manifest && manifest.c2),
    prior: prior !== undefined ? prior : (manifest && manifest.prior),
  };

  if (isExplicitDynamic) {
    const dynTarget = computeDynamicTargetQuestions(manifest.topics, budgetOptions);
    manifest = {
      ...manifest,
      target_questions: dynTarget,
      is_dynamic_target: true,
      target_questions_mode: 'dynamic',
      topics: apportionQuotas(manifest.topics, dynTarget, budgetOptions)
    };
  } else if (hasManualTarget) {
    const numericTarget = parseInt(rawTarget, 10);
    if (isNaN(numericTarget)) {
      if (req.body.strict) {
        return res.status(400).json({
          error: `Invalid manual target_questions: '${rawTarget}'. Must be an integer between ${MIN_MANUAL_QUESTIONS} and ${MAX_MANUAL_QUESTIONS}.`
        });
      }
      const dynTarget = computeDynamicTargetQuestions(manifest.topics, budgetOptions);
      manifest = {
        ...manifest,
        target_questions: dynTarget,
        is_dynamic_target: true,
        target_questions_mode: 'dynamic',
        topics: apportionQuotas(manifest.topics, dynTarget, budgetOptions)
      };
    } else {
      let finalTarget = numericTarget;
      const shouldClamp = req.body.clamp_manual !== false && req.body.clamp !== false;
      if (numericTarget < MIN_MANUAL_QUESTIONS || numericTarget > MAX_MANUAL_QUESTIONS) {
        if (req.body.strict === true || (!shouldClamp && req.body.validate_range === true)) {
          return res.status(400).json({
            error: `Manual target questions must be between ${MIN_MANUAL_QUESTIONS} and ${MAX_MANUAL_QUESTIONS} (inclusive), got ${numericTarget}.`
          });
        }
        if (req.body.validate_range === true || req.body.clamp_manual === true || req.body.clamp === true) {
          finalTarget = Math.max(MIN_MANUAL_QUESTIONS, Math.min(MAX_MANUAL_QUESTIONS, numericTarget));
        } else {
          finalTarget = Math.max(0, numericTarget);
        }
      }
      manifest = {
        ...manifest,
        target_questions: finalTarget,
        is_dynamic_target: false,
        target_questions_mode: 'manual',
        topics: apportionQuotas(manifest.topics, finalTarget, budgetOptions)
      };
    }
  } else if (manifest.topics.length > 0 && manifest.topics.every(t => t.allocated_quota === undefined || t.allocated_quota === null)) {
    // Default to fully dynamic mode when no manual choice was made and topics lack quotas
    const dynTarget = computeDynamicTargetQuestions(manifest.topics, budgetOptions);
    manifest = {
      ...manifest,
      target_questions: dynTarget,
      is_dynamic_target: true,
      target_questions_mode: 'dynamic',
      topics: apportionQuotas(manifest.topics, dynTarget, budgetOptions)
    };
  }

  try {
    const topicOutputs = [];
    let combinedExam = `# CBSE CLASS ${grade} EXAMINATION: ${subject.toUpperCase()}\n## CHAPTER: ${chapter_title.toUpperCase()}\n\n`;
    let overallQNum = 1;

    for (const t of manifest.topics) {
      const topicName = t.topic_name;
      const quota = t.allocated_quota !== undefined ? t.allocated_quota : 1;
      if (quota <= 0) continue;

      const weight = t.final_weight !== undefined ? t.final_weight : 0.0;
      const diff = t.difficulty_multiplier || 1.0;

      // Find matching chunk text
      let chunkText = '';
      if (Array.isArray(chunks)) {
        for (const c of chunks) {
          const cTopic = typeof c === 'object' ? (c.topic || c.title || '') : '';
          const cText = typeof c === 'object' ? (c.text || c.latex || c.content || '') : String(c);
          if (cTopic.toLowerCase().includes(topicName.toLowerCase()) || cText.toLowerCase().includes(topicName.toLowerCase())) {
            chunkText = cText;
            break;
          }
        }
      }
      if (!chunkText && chunks && chunks.length > 0) {
        chunkText = typeof chunks[0] === 'object' ? (chunks[0].text || chunks[0].latex || '') : String(chunks[0]);
      }

      // Build selection according to quota, difficulty, and weight
      let sel = t.requested_selection;
      if (!sel || ['auto', 'dynamic', 'none'].includes(String(sel).trim().toLowerCase()) || String(sel).includes('Select at least one')) {
        sel = buildRequestedSelection(quota, diff, weight);
      }

      let topicOutput = '';
      const effectiveKey = apiKey || process.env.GEMINI_API_KEY;
      if (!useMock && effectiveKey) {
        try {
          const userPrompt = `INPUT PARAMETERS:
- {subject}: ${subject}
- {grade}: ${grade}
- {chapter_title}: ${chapter_title}
- {topic_name}: ${topicName}
- {topic_weight}: ${weight}
- {allocated_quota}: ${quota}
- {requested_selection}: ${sel}

LATEX CHUNK:
\`\`\`latex
${chunkText}
\`\`\`

Strictly obey all guidelines in the system context. Generate ONLY the requested questions following the exact output structure.`;

          const requestBody = {
            system_instruction: { parts: [{ text: SYSTEM_PROMPT }] },
            contents: [{ role: 'user', parts: [{ text: userPrompt }] }],
            generationConfig: { temperature: 0.2, topP: 0.95 }
          };

          const candidateModels = [
            process.env.GEMINI_MODEL,
            'gemini-flash-lite-latest',
            'gemini-3.5-flash-lite',
            'gemini-3.5-flash',
            'gemini-3.7-flash',
            'gemini-3.1-flash-lite',
            'gemini-flash-latest',
            'gemini-3.8-flash',
            'gemini-2.5-flash',
            'gemini-1.5-flash'
          ].filter(Boolean);
          for (const modelName of candidateModels) {
            try {
              const gRes = await fetch(`https://generativelanguage.googleapis.com/v1beta/models/${modelName}:generateContent?key=${effectiveKey}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestBody)
              });
              if (gRes.ok) {
                const gData = await gRes.json();
                topicOutput = gData.candidates?.[0]?.content?.parts?.[0]?.text || '';
                if (topicOutput.trim()) break;
              }
            } catch (e) {
              // Try next model
            }
          }
        } catch (e) {
          // Fall through to mock generator
        }
      }

      if (!topicOutput.trim()) {
        topicOutput = generateMockResponse(
          subject,
          grade,
          chapter_title,
          topicName,
          sel,
          chunkText,
          weight,
          quota,
          overallQNum
        );
      } else {
        // Renumber live output sequentially
        const qMatches = [...topicOutput.matchAll(/###\s*Q(\d+)/g)];
        if (qMatches.length > 0) {
          for (let idx = qMatches.length - 1; idx >= 0; idx--) {
            const m = qMatches[idx];
            const assigned = overallQNum + idx;
            topicOutput = topicOutput.slice(0, m.index) + `### Q${assigned}` + topicOutput.slice(m.index + m[0].length);
          }
        }
      }

      const countMatches = topicOutput.match(/###\s*Q\d+/g) || [];
      const generatedCount = countMatches.length || quota;
      overallQNum += generatedCount;

      topicOutputs.push({
        topic_name: topicName,
        final_weight: weight,
        allocated_quota: quota,
        requested_selection: sel,
        output: topicOutput
      });

      combinedExam += `\n### Section: ${topicName} (Weight: ${(weight * 100).toFixed(1)}%, Quota: ${quota})\n\n${topicOutput}\n\n---\n`;
    }

    res.json({
      success: true,
      chapter_title,
      subject,
      grade,
      manifest,
      target_questions: manifest.target_questions,
      is_dynamic_target: manifest.is_dynamic_target,
      target_questions_mode: manifest.target_questions_mode,
      topics: topicOutputs,
      total_questions_generated: overallQNum - 1,
      combined_exam: combinedExam
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// 6. Dynamic / Manual Target Questions Budgeting Endpoint
app.post('/api/dynamic-target', (req, res) => {
  const {
    manifest,
    topics,
    min_questions,
    max_questions,
    c1,
    c2,
    prior,
    target_questions,
    mode,
    strict = false,
    clamp = true,
  } = req.body;
  const inputTopics = (manifest && manifest.topics) || topics;
  if (!inputTopics || !Array.isArray(inputTopics)) {
    return res.status(400).json({ error: 'Valid manifest with topics or topics array is required' });
  }

  // Dual mode logic:
  // Default: Dynamic mode (when target_questions is not specified or explicitly 'auto'/'dynamic')
  // Manual mode: User specifies an explicit integer total between 10 and 50
  const rawVal = (target_questions !== undefined && target_questions !== null && target_questions !== '')
    ? target_questions
    : (manifest && manifest.target_questions !== undefined && manifest.target_questions !== null && manifest.target_questions !== '' ? manifest.target_questions : undefined);

  const isExplicitDynamic = mode === 'dynamic' ||
    (typeof rawVal === 'string' && ['auto', 'dynamic'].includes(rawVal.trim().toLowerCase())) ||
    (manifest && manifest.is_dynamic_target === true && rawVal === undefined);

  const hasManualChoice = !isExplicitDynamic && rawVal !== undefined;

  let targetQuestions;
  let isDynamic = true;
  let targetMode = 'dynamic';

  if (hasManualChoice) {
    const numericTarget = parseInt(rawVal, 10);

    if (isNaN(numericTarget)) {
      if (strict) {
        return res.status(400).json({ error: `Invalid manual target_questions: '${rawVal}'. Must be an integer between ${MIN_MANUAL_QUESTIONS} and ${MAX_MANUAL_QUESTIONS}.` });
      }
      targetQuestions = computeDynamicTargetQuestions(inputTopics, { min_questions, max_questions, c1, c2, prior });
      isDynamic = true;
      targetMode = 'dynamic';
    } else {
      if (numericTarget < MIN_MANUAL_QUESTIONS || numericTarget > MAX_MANUAL_QUESTIONS) {
        if (strict || clamp === false) {
          return res.status(400).json({
            error: `Manual target questions must be between ${MIN_MANUAL_QUESTIONS} and ${MAX_MANUAL_QUESTIONS} (inclusive), got ${numericTarget}.`
          });
        }
        targetQuestions = Math.max(MIN_MANUAL_QUESTIONS, Math.min(MAX_MANUAL_QUESTIONS, numericTarget));
      } else {
        targetQuestions = numericTarget;
      }
      isDynamic = false;
      targetMode = 'manual';
    }
  } else {
    targetQuestions = computeDynamicTargetQuestions(inputTopics, { min_questions, max_questions, c1, c2, prior });
    isDynamic = true;
    targetMode = 'dynamic';
  }

  const updatedTopics = apportionQuotas(inputTopics, targetQuestions, { prior });

  let updatedManifest = null;
  if (manifest) {
    updatedManifest = {
      ...manifest,
      target_questions: targetQuestions,
      is_dynamic_target: isDynamic,
      target_questions_mode: targetMode,
      topics: updatedTopics
    };
  }

  res.json({
    success: true,
    target_questions: targetQuestions,
    is_dynamic_target: isDynamic,
    target_questions_mode: targetMode,
    topics: updatedTopics,
    manifest: updatedManifest
  });
});

if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`CBSE Question Generator POC server running on http://localhost:${PORT}`);
  });
}

module.exports = {
  app,
  computeDynamicTargetQuestions,
  apportionQuotas,
  buildRequestedSelection,
  parseSelectionCounts,
  MIN_MANUAL_QUESTIONS,
  MAX_MANUAL_QUESTIONS
};
