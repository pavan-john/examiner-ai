// Automated verification script for CBSE Question Generator POC
const { spawn } = require('child_process');
const path = require('path');
const { buildRequestedSelection, parseSelectionCounts } = require('./server');

async function isServerRunning() {
  try {
    const res = await fetch('http://localhost:3000/api/samples');
    return res.ok;
  } catch {
    return false;
  }
}

async function runTests() {
  console.log('--- Starting CBSE Generator API Tests ---');

  let serverProcess = null;
  const running = await isServerRunning();
  if (!running) {
    console.log('Server not detected on port 3000. Launching temporary server instance...');
    serverProcess = spawn(process.execPath, [path.join(__dirname, 'server.js')], {
      cwd: __dirname,
      stdio: 'ignore'
    });

    let ready = false;
    for (let i = 0; i < 30; i++) {
      await new Promise(resolve => setTimeout(resolve, 200));
      if (await isServerRunning()) {
        ready = true;
        break;
      }
    }
    if (!ready) {
      if (serverProcess) serverProcess.kill();
      throw new Error('Failed to start server on port 3000 within 6 seconds.');
    }
    console.log('Temporary server successfully started.');
  }

  try {
    // Test 1: Fetch samples
    const samplesRes = await fetch('http://localhost:3000/api/samples');
    const samplesData = await samplesRes.json();
    console.log(`[PASS] Samples count: ${samplesData.samples.length}`);
    if (samplesData.samples.length < 3) throw new Error('Expected at least 3 samples');

    // Test 2: Split chunk
    const sampleTex = `\\section{Prism Formula}\nLight bends through prism.\n\\subsection{Minimum Deviation}\nAngle of minimum deviation is Dm.`;
    const splitRes = await fetch('http://localhost:3000/api/split-chunk', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ latex: sampleTex })
    });
    const splitData = await splitRes.json();
    console.log(`[PASS] Detected sections: ${splitData.topics.length}`);
    if (splitData.topics.length !== 2) throw new Error('Expected 2 detected sections');

    // Test 3a: Generate in Mock mode (Physics)
    const genRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: 'Physics',
        grade: '12',
        chapter_title: 'Ray Optics and Optical Instruments',
        topic_name: 'Refraction through a Prism',
        latex_chunk: samplesData.samples[0].latex_chunk,
        requested_selection: '2 MCQs, 1 Assertion-Reason, 1 Short Answer (3M), 1 Long Answer (5M)',
        useMock: true
      })
    });
    const genData = await genRes.json();
    console.log(`[PASS] Mock Physics generation status: ${genData.success}, mode: ${genData.mode}`);
    if (!genData.output.includes('### Q1') || !genData.output.includes('Marking Scheme & Solution:')) {
      throw new Error('Generated output does not match CBSE template format');
    }

    // Test 3b: Chemistry (Nernst equation)
    const chemRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: samplesData.samples[1].subject,
        grade: samplesData.samples[1].grade,
        chapter_title: samplesData.samples[1].chapter_title,
        topic_name: samplesData.samples[1].topic_name,
        latex_chunk: samplesData.samples[1].latex_chunk,
        requested_selection: '1 MCQ, 1 Assertion-Reason, 1 Very Short Answer (2M), 1 Case-Based (4M)',
        useMock: true
      })
    });
    const chemData = await chemRes.json();
    console.log(`[PASS] Mock Chemistry generation status: ${chemData.success}, mode: ${chemData.mode}`);
    if (!chemData.output.includes('Nernst') && !chemData.output.includes('Daniell')) {
      throw new Error('Chemistry generation output missing expected content');
    }

    // Test 3c: Mathematics (Integration by parts)
    const mathRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: samplesData.samples[2].subject,
        grade: samplesData.samples[2].grade,
        chapter_title: samplesData.samples[2].chapter_title,
        topic_name: samplesData.samples[2].topic_name,
        latex_chunk: samplesData.samples[2].latex_chunk,
        requested_selection: '1 MCQ, 1 Short Answer (3M), 1 Long Answer / Derivation (5M)',
        useMock: true
      })
    });
    const mathData = await mathRes.json();
    console.log(`[PASS] Mock Math generation status: ${mathData.success}, mode: ${mathData.mode}`);
    if (!mathData.output.includes('ILATE') && !mathData.output.includes('parts')) {
      throw new Error('Math generation output missing expected content');
    }

    // Test 4: Static assets check
    const htmlRes = await fetch('http://localhost:3000/index.html');
    const htmlText = await htmlRes.text();
    if (!htmlText.includes('CBSE Question Generator')) {
      throw new Error('index.html missing title');
    }
    console.log('[PASS] Static index.html served correctly');

    // Test 5: Chapter orchestration endpoints (/api/toc, /api/chunks, /api/questions)
    const tocRes = await fetch('http://localhost:3000/api/toc');
    const tocData = await tocRes.json();
    console.log(`[PASS] TOC topics count: ${tocData.topics.length}`);
    if (!tocData.topics || tocData.topics.length !== 3) {
      throw new Error('Expected 3 TOC topics for Electromagnetic Induction');
    }

    const chunksRes = await fetch('http://localhost:3000/api/chunks');
    const chunksData = await chunksRes.json();
    console.log(`[PASS] Chunks count: ${chunksData.length}`);
    if (!chunksData || chunksData.length !== 3) {
      throw new Error('Expected 3 chunks for Electromagnetic Induction');
    }

    const questionsRes = await fetch('http://localhost:3000/api/questions');
    const questionsData = await questionsRes.json();
    console.log(`[PASS] Questions count: ${questionsData.questions.length}`);
    if (!questionsData.questions || questionsData.questions.length !== 12) {
      throw new Error('Expected 12 questions for Electromagnetic Induction');
    }

    // Test 6: Fetch prompt template endpoint
    const promptRes = await fetch('http://localhost:3000/api/prompt-template');
    const promptData = await promptRes.json();
    if (!promptData.success || !promptData.systemPrompt || !promptData.userPromptTemplate) {
      throw new Error('Failed to fetch prompt template');
    }
    if (!promptData.systemPrompt.includes('CBSE QUESTION GENERATOR') || !promptData.systemPrompt.includes('{topic_weight}')) {
      throw new Error('Prompt template missing expected CBSE context or topic_weight parameter');
    }
    console.log('[PASS] Prompt template endpoint verified');

    // Test 7: Generate with topic weight and allocated quota
    const weightedGenRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: 'Physics',
        grade: '12',
        chapter_title: 'Electromagnetic Induction',
        topic_name: "Faraday's Law of Induction",
        topic_weight: 0.5945,
        allocated_quota: 4,
        latex_chunk: chunksData[0].text,
        requested_selection: '1 MCQ, 1 Short Answer (3M), 1 Long Answer / Derivation (5M)',
        useMock: true
      })
    });
    const weightedGenData = await weightedGenRes.json();
    if (!weightedGenData.success || !weightedGenData.output.includes("Faraday") || !weightedGenData.output.includes("### Q1")) {
      throw new Error('Weighted generation for Faraday topic failed');
    }
    if (!weightedGenData.promptUsed.userPrompt.includes('{topic_weight}: 0.5945')) {
      throw new Error('User prompt did not record topic_weight');
    }
    console.log('[PASS] Weighted question generation endpoint verified');

    // Test 8: Generate exam from chapter manifest
    const manifestExamRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", final_weight: 0.5945, allocated_quota: 3, difficulty_multiplier: 1.85 },
            { topic_name: "Lenz's Law", final_weight: 0.1913, allocated_quota: 1, difficulty_multiplier: 1.25 },
            { topic_name: "Eddy Currents", final_weight: 0.2142, allocated_quota: 1, difficulty_multiplier: 1.40 }
          ]
        },
        chunks: chunksData,
        subject: 'Physics',
        grade: '12',
        chapter_title: 'Electromagnetic Induction',
        useMock: true
      })
    });
    const manifestExamData = await manifestExamRes.json();
    if (!manifestExamData.success || manifestExamData.topics.length !== 3 || !manifestExamData.combined_exam.includes('Section: Faraday')) {
      throw new Error('Manifest-based exam generation failed');
    }
    console.log('[PASS] Full manifest exam generation verified');

    // Test 9: Verify sequential numbering across topics (no duplicate Q1s)
    const qMatches = [...manifestExamData.combined_exam.matchAll(/###\s*Q(\d+)/g)].map(m => parseInt(m[1], 10));
    console.log(`[PASS] Number of generated exam questions: ${qMatches.length}, sequence: ${qMatches.join(', ')}`);
    if (qMatches.length !== 5) {
      throw new Error(`Expected exactly 5 questions across topics, got ${qMatches.length}`);
    }
    for (let i = 0; i < qMatches.length; i++) {
      if (qMatches[i] !== i + 1) {
        throw new Error(`Expected sequential Q${i + 1}, but found Q${qMatches[i]}`);
      }
    }
    console.log('[PASS] Sequential question numbering (Q1 to Q5) verified');

    // Test 10: Skipping zero quota topics in generate-weighted
    const zeroQuotaRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", final_weight: 0.70, allocated_quota: 2, difficulty_multiplier: 1.85 },
            { topic_name: "Lenz's Law", final_weight: 0.00, allocated_quota: 0, difficulty_multiplier: 1.25 },
            { topic_name: "Eddy Currents", final_weight: 0.30, allocated_quota: 2, difficulty_multiplier: 1.40 }
          ]
        },
        chunks: chunksData,
        subject: 'Physics',
        grade: '12',
        chapter_title: 'Electromagnetic Induction',
        useMock: true
      })
    });
    const zeroQuotaData = await zeroQuotaRes.json();
    const zeroMatches = [...zeroQuotaData.combined_exam.matchAll(/###\s*Q(\d+)/g)].map(m => parseInt(m[1], 10));
    if (zeroMatches.length !== 4) {
      throw new Error(`Expected exactly 4 questions when skipping quota 0, got ${zeroMatches.length}`);
    }
    if (zeroMatches.join(',') !== '1,2,3,4') {
      throw new Error(`Expected sequence 1,2,3,4 but got ${zeroMatches.join(',')}`);
    }
    if (zeroQuotaData.topics.some(t => t.topic_name === "Lenz's Law")) {
      throw new Error('Expected zero quota topic to be excluded from generated topic sections');
    }
    console.log('[PASS] Zero-quota topic skipping verified');

    // Test 11: Exact category match: "1 Very Short Answer (2M)" must not produce Short Answer (3M)
    const vsaOnlyRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: 'Physics',
        grade: '12',
        chapter_title: 'Electromagnetic Induction',
        topic_name: "Faraday's Law of Induction",
        latex_chunk: chunksData[0].text,
        requested_selection: '1 Very Short Answer (2M)',
        useMock: true
      })
    });
    const vsaOnlyData = await vsaOnlyRes.json();
    if (!vsaOnlyData.output.includes('Very Short Answer (VSA) | 2 Mark(s)')) {
      throw new Error('VSA generation missing Very Short Answer header');
    }
    if (vsaOnlyData.output.includes('Short Answer (SA) | 3 Mark(s)')) {
      throw new Error('VSA selection mistakenly generated Short Answer (3M) question due to substring collision');
    }
    console.log('[PASS] Exact category selection (VSA vs SA) verified');

    // Test 12: /api/status endpoint
    const statusRes = await fetch('http://localhost:3000/api/status');
    const statusData = await statusRes.json();
    if (!statusData.success || !statusData.hasEnvKey) {
      throw new Error(`Expected /api/status to return success=true and hasEnvKey=true, got ${JSON.stringify(statusData)}`);
    }
    console.log(`[PASS] API status verified: hasEnvKey=${statusData.hasEnvKey}, model=${statusData.model}`);

    // Test 13: /api/verify-key endpoint
    const verifyKeyRes = await fetch('http://localhost:3000/api/verify-key', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ apiKey: process.env.GEMINI_API_KEY || '' })
    });
    const verifyKeyData = await verifyKeyRes.json();
    if (!verifyKeyData.success) {
      throw new Error(`/api/verify-key failed: ${verifyKeyData.error}`);
    }
    console.log(`[PASS] API verify-key verified: success=${verifyKeyData.success}, model=${verifyKeyData.model}`);

    // Test 14: /api/dynamic-target endpoint
    const dynamicTargetRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        }
      })
    });
    const dynamicTargetData = await dynamicTargetRes.json();
    if (!dynamicTargetData.success || dynamicTargetData.target_questions !== 5 || !dynamicTargetData.is_dynamic_target) {
      throw new Error(`Expected /api/dynamic-target to return target_questions=5, got ${JSON.stringify(dynamicTargetData)}`);
    }
    const allocatedSum = dynamicTargetData.topics.reduce((acc, t) => acc + t.allocated_quota, 0);
    if (allocatedSum !== 5) {
      throw new Error(`Expected sum of allocated quotas to equal dynamic target 5, got ${allocatedSum}`);
    }
    console.log(`[PASS] API dynamic-target verified: target_questions=${dynamicTargetData.target_questions}, quotas=${dynamicTargetData.topics.map(t => t.allocated_quota).join(',')}`);

    // Test 15: /api/generate-weighted with dynamic_target: true
    const dynGenRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        chunks: chunksData,
        subject: 'Physics',
        grade: '12',
        chapter_title: 'Electromagnetic Induction',
        dynamic_target: true,
        useMock: true
      })
    });
    const dynGenData = await dynGenRes.json();
    if (!dynGenData.success || dynGenData.total_questions_generated !== 5) {
      throw new Error(`Expected 5 questions with dynamic_target=true, got ${dynGenData.total_questions_generated}`);
    }
    console.log(`[PASS] generate-weighted with dynamic_target: true verified (generated ${dynGenData.total_questions_generated} questions)`);

    // Test 16: /api/generate-weighted with manifest.target_questions = 'auto'
    const autoGenRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          target_questions: 'auto',
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        chunks: chunksData,
        subject: 'Physics',
        grade: '12',
        chapter_title: 'Electromagnetic Induction',
        useMock: true
      })
    });
    const autoGenData = await autoGenRes.json();
    if (!autoGenData.success || autoGenData.total_questions_generated !== 5) {
      throw new Error(`Expected 5 questions with manifest.target_questions='auto', got ${autoGenData.total_questions_generated}`);
    }
    console.log(`[PASS] generate-weighted with manifest.target_questions='auto' verified`);

    // Test 17: /api/generate-weighted with case-insensitive 'AUTO' and min_questions bound
    const caseGenRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        chunks: chunksData,
        target_questions: 'AUTO',
        min_questions: 6,
        useMock: true
      })
    });
    const caseGenData = await caseGenRes.json();
    if (!caseGenData.success || caseGenData.total_questions_generated !== 6) {
      throw new Error(`Expected 6 questions with case-insensitive 'AUTO' and min_questions=6, got ${caseGenData.total_questions_generated}`);
    }
    console.log(`[PASS] generate-weighted with target_questions='AUTO' and min_questions=6 verified`);

    // Test 18: /api/dynamic-target with corrupt / non-numeric data (ensuring no NaN)
    const corruptRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topics: [
          { topic_name: "Topic 1", question_impact_score: "not_a_number", difficulty_multiplier: "invalid" },
          { topic_name: "Topic 2", question_impact_score: null, difficulty_multiplier: null },
          { topic_name: "Topic 3", question_impact_score: 8.0, difficulty_multiplier: 1.5 }
        ],
        min_questions: 3
      })
    });
    const corruptData = await corruptRes.json();
    if (!corruptData.success || typeof corruptData.target_questions !== 'number' || isNaN(corruptData.target_questions)) {
      throw new Error(`Expected valid numeric target_questions from corrupt input, got ${JSON.stringify(corruptData)}`);
    }
    const corruptAllocSum = corruptData.topics.reduce((acc, t) => acc + t.allocated_quota, 0);
    if (corruptAllocSum !== corruptData.target_questions) {
      throw new Error(`Corrupt input allocated quotas sum (${corruptAllocSum}) did not match target (${corruptData.target_questions})`);
    }
    console.log(`[PASS] API dynamic-target with corrupt / dirty input verified (no NaN, sum=${corruptAllocSum})`);

    // Test 19: /api/generate-weighted with numeric string 'target_questions: "4"'
    const strTargetRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        chunks: chunksData,
        target_questions: "4",
        useMock: true
      })
    });
    const strTargetData = await strTargetRes.json();
    if (!strTargetData.success || strTargetData.total_questions_generated !== 4) {
      throw new Error(`Expected 4 questions with target_questions="4", got ${strTargetData.total_questions_generated}`);
    }
    console.log(`[PASS] generate-weighted with string target_questions="4" verified`);

    // Test 20: Dual mode - /api/dynamic-target default dynamic mode
    const dualDynRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        }
      })
    });
    const dualDynData = await dualDynRes.json();
    if (!dualDynData.success || !dualDynData.is_dynamic_target || dualDynData.target_questions_mode !== 'dynamic') {
      throw new Error(`Expected default dynamic mode from /api/dynamic-target, got ${JSON.stringify(dualDynData)}`);
    }
    console.log(`[PASS] Dual mode: default dynamic mode verified (target=${dualDynData.target_questions})`);

    // Test 21: Dual mode - /api/dynamic-target manual selection within [10, 50]
    const manualTargetRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        target_questions: 25
      })
    });
    const manualTargetData = await manualTargetRes.json();
    if (!manualTargetData.success || manualTargetData.target_questions !== 25 || manualTargetData.is_dynamic_target !== false || manualTargetData.target_questions_mode !== 'manual') {
      throw new Error(`Expected manual target 25, got ${JSON.stringify(manualTargetData)}`);
    }
    const manualAllocSum = manualTargetData.topics.reduce((acc, t) => acc + t.allocated_quota, 0);
    if (manualAllocSum !== 25) {
      throw new Error(`Expected quotas sum 25, got ${manualAllocSum}`);
    }
    console.log(`[PASS] Dual mode: manual selection 25 questions with dynamic topic apportionment verified (sum=${manualAllocSum})`);

    // Test 22: Dual mode - /api/dynamic-target manual target < 10 clamped to 10
    const clampLowRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        target_questions: 5
      })
    });
    const clampLowData = await clampLowRes.json();
    if (clampLowData.target_questions !== 10 || clampLowData.target_questions_mode !== 'manual') {
      throw new Error(`Expected target clamped to 10, got ${JSON.stringify(clampLowData)}`);
    }
    const clampLowSum = clampLowData.topics.reduce((acc, t) => acc + t.allocated_quota, 0);
    if (clampLowSum !== 10) {
      throw new Error(`Expected clamped quotas sum 10, got ${clampLowSum}`);
    }
    console.log(`[PASS] Dual mode: manual target 5 clamped to 10 verified`);

    // Test 23: Dual mode - /api/dynamic-target manual target > 50 clamped to 50
    const clampHighRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        target_questions: 65
      })
    });
    const clampHighData = await clampHighRes.json();
    if (clampHighData.target_questions !== 50 || clampHighData.target_questions_mode !== 'manual') {
      throw new Error(`Expected target clamped to 50, got ${JSON.stringify(clampHighData)}`);
    }
    const clampHighSum = clampHighData.topics.reduce((acc, t) => acc + t.allocated_quota, 0);
    if (clampHighSum !== 50) {
      throw new Error(`Expected clamped quotas sum 50, got ${clampHighSum}`);
    }
    console.log(`[PASS] Dual mode: manual target 65 clamped to 50 verified`);

    // Test 24: Dual mode - /api/dynamic-target strict validation returns 400 error
    const strictRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topics: [
          { topic_name: "Topic 1", question_impact_score: 5.0, difficulty_multiplier: 1.2 }
        ],
        target_questions: 70,
        strict: true
      })
    });
    if (strictRes.status !== 400) {
      throw new Error(`Expected 400 status for strict out-of-range target, got ${strictRes.status}`);
    }
    console.log('[PASS] Dual mode: strict validation out-of-range returns 400 error verified');

    // Test 25: Dual mode - /api/generate-weighted with manual target_questions=20
    const manualGenRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        chunks: chunksData,
        target_questions: 20,
        validate_range: true,
        useMock: true
      })
    });
    const manualGenData = await manualGenRes.json();
    if (!manualGenData.success || manualGenData.total_questions_generated !== 20) {
      throw new Error(`Expected 20 questions generated in manual mode, got ${manualGenData.total_questions_generated}`);
    }
    if (manualGenData.manifest.target_questions_mode !== 'manual' || manualGenData.manifest.is_dynamic_target !== false) {
      throw new Error(`Expected manual target mode in manifest, got ${JSON.stringify(manualGenData.manifest)}`);
    }
    console.log(`[PASS] Dual mode: generate-weighted with manual target 20 verified (${manualGenData.total_questions_generated} questions)`);

    // Test 26: Dual mode - /api/generate-weighted default dynamic mode on unallocated manifest
    const unallocatedGenRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        chunks: chunksData,
        useMock: true
      })
    });
    const unallocatedGenData = await unallocatedGenRes.json();
    if (!unallocatedGenData.success || unallocatedGenData.manifest.target_questions_mode !== 'dynamic') {
      throw new Error(`Expected default dynamic mode on unallocated manifest, got ${JSON.stringify(unallocatedGenData.manifest)}`);
    }
    console.log(`[PASS] Dual mode: generate-weighted unallocated manifest defaults to dynamic mode (${unallocatedGenData.total_questions_generated} questions)`);

    // Test 27: /api/generate-weighted with manifest.target_questions=25 (without top-level target_questions)
    const manifestTargetRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          target_questions: 25,
          topics: [
            { topic_name: "Faraday's Law of Induction", question_impact_score: 10.0, difficulty_multiplier: 1.85, final_weight: 0.5945 },
            { topic_name: "Lenz's Law", question_impact_score: 4.5, difficulty_multiplier: 1.25, final_weight: 0.1913 },
            { topic_name: "Eddy Currents", question_impact_score: 4.5, difficulty_multiplier: 1.40, final_weight: 0.2142 }
          ]
        },
        chunks: chunksData,
        useMock: true
      })
    });
    const manifestTargetData = await manifestTargetRes.json();
    if (!manifestTargetData.success || manifestTargetData.manifest.target_questions !== 25 || manifestTargetData.manifest.target_questions_mode !== 'manual') {
      throw new Error(`Expected manual target 25 from manifest.target_questions, got ${JSON.stringify(manifestTargetData.manifest)}`);
    }
    const manifestQuotaSum = manifestTargetData.manifest.topics.reduce((acc, t) => acc + t.allocated_quota, 0);
    if (manifestQuotaSum !== 25) {
      throw new Error(`Expected quotas sum 25, got ${manifestQuotaSum}`);
    }
    console.log(`[PASS] Dual mode: generate-weighted preserves manifest.target_questions=25 with dynamic quotas sum=25`);

    // Test 28: /api/dynamic-target with string manifest.target_questions="25"
    const strManifestRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: {
          target_questions: "25",
          topics: [
            { topic_name: "Topic 1", question_impact_score: 10.0, difficulty_multiplier: 1.5 },
            { topic_name: "Topic 2", question_impact_score: 5.0, difficulty_multiplier: 1.0 }
          ]
        }
      })
    });
    const strManifestData = await strManifestRes.json();
    if (!strManifestData.success || strManifestData.target_questions !== 25 || strManifestData.target_questions_mode !== 'manual') {
      throw new Error(`Expected manual target 25 from string manifest.target_questions, got ${JSON.stringify(strManifestData)}`);
    }
    console.log(`[PASS] Dual mode: dynamic-target handles string manifest.target_questions="25"`);

    // Test 29: /api/dynamic-target with target_questions=5 and clamp=false returns HTTP 400
    const noClampRes = await fetch('http://localhost:3000/api/dynamic-target', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        topics: [{ topic_name: "Topic 1" }, { topic_name: "Topic 2" }],
        target_questions: 5,
        clamp: false
      })
    });
    if (noClampRes.status !== 400) {
      throw new Error(`Expected status 400 for out-of-range target with clamp=false, got ${noClampRes.status}`);
    }
    console.log(`[PASS] Dual mode: dynamic-target with clamp=false rejects out-of-range target with 400`);

    // Test 30: /api/generate-weighted with target_questions=5 and strict=true returns HTTP 400
    const strictGenRes = await fetch('http://localhost:3000/api/generate-weighted', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        manifest: { topics: [{ topic_name: "T1" }, { topic_name: "T2" }] },
        target_questions: 5,
        strict: true
      })
    });
    if (strictGenRes.status !== 400) {
      throw new Error(`Expected status 400 for out-of-range target with strict=true, got ${strictGenRes.status}`);
    }
    console.log(`[PASS] Dual mode: generate-weighted with strict=true rejects out-of-range target with 400`);

    // Test 31: /api/generate returns target_questions and target_questions_mode
    const genSingleRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: 'Physics',
        latex_chunk: 'Sample chunk',
        requested_selection: '2 MCQs',
        target_questions: 25,
        useMock: true
      })
    });
    const genSingleData = await genSingleRes.json();
    if (!genSingleData.success || genSingleData.target_questions !== 25 || genSingleData.target_questions_mode !== 'manual') {
      throw new Error(`Expected target_questions: 25 and mode: 'manual' from /api/generate, got ${JSON.stringify(genSingleData)}`);
    }
    console.log(`[PASS] Dual mode: /api/generate returns target_questions=25 and target_questions_mode='manual'`);

    // Test 32: buildRequestedSelection unit test
    const autoSelHigh = buildRequestedSelection(2, 1.85, 0.5);
    if (!autoSelHigh.includes('Short Answer') || !autoSelHigh.includes('Derivation')) {
      throw new Error(`Expected High Rigor selection to include Short Answer and Derivation, got: ${autoSelHigh}`);
    }
    const autoSelLow = buildRequestedSelection(2, 1.1, 0.2);
    if (!autoSelLow.includes('MCQ') || !autoSelLow.includes('Very Short Answer')) {
      throw new Error(`Expected Low Rigor selection to include MCQ and Very Short Answer, got: ${autoSelLow}`);
    }
    console.log(`[PASS] Dynamic Taxonomy: buildRequestedSelection calibrated to rigor correctly`);

    // Test 33: /api/generate with omitted requested_selection defaults to dynamic selection
    const genAutoOmittedRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: 'Physics',
        latex_chunk: '\\section{Electromagnetic Induction}\nInduced EMF is proportional to change in flux.',
        useMock: true
      })
    });
    const genAutoOmittedData = await genAutoOmittedRes.json();
    if (!genAutoOmittedData.success || genAutoOmittedData.selection_mode !== 'dynamic') {
      throw new Error(`Expected success and selection_mode: 'dynamic' when requested_selection is omitted, got ${JSON.stringify(genAutoOmittedData)}`);
    }
    if (!genAutoOmittedData.requested_selection || !genAutoOmittedData.output.includes('### Q1')) {
      throw new Error(`Expected valid dynamically constructed question output when requested_selection omitted`);
    }
    console.log(`[PASS] Dynamic Taxonomy: /api/generate defaults to dynamic auto-selection when omitted`);

    // Test 34: /api/generate with requested_selection: 'auto'
    const genAutoExplicitRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: 'Physics',
        latex_chunk: '\\section{Faraday Law}\nFlux change induces EMF.',
        requested_selection: 'auto',
        allocated_quota: 2,
        difficulty_multiplier: 1.8,
        useMock: true
      })
    });
    const genAutoExplicitData = await genAutoExplicitRes.json();
    if (!genAutoExplicitData.success || genAutoExplicitData.selection_mode !== 'dynamic') {
      throw new Error(`Expected success and selection_mode: 'dynamic' when requested_selection='auto', got ${JSON.stringify(genAutoExplicitData)}`);
    }
    if (!genAutoExplicitData.requested_selection.includes('Derivation') && !genAutoExplicitData.requested_selection.includes('Short Answer')) {
      throw new Error(`Expected high difficulty to generate Derivation/Short Answer, got: ${genAutoExplicitData.requested_selection}`);
    }
    console.log(`[PASS] Dynamic Taxonomy: /api/generate with requested_selection='auto' selects based on difficulty`);

    // Test 35: /api/generate preserves manual selection when explicitly provided
    const genManualExplicitRes = await fetch('http://localhost:3000/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        subject: 'Chemistry',
        latex_chunk: 'Nernst equation formulas.',
        requested_selection: '1 MCQ, 1 Case-Based (4M)',
        useMock: true
      })
    });
    const genManualExplicitData = await genManualExplicitRes.json();
    if (!genManualExplicitData.success || genManualExplicitData.selection_mode !== 'manual') {
      throw new Error(`Expected selection_mode: 'manual' when explicit selection string passed, got: ${JSON.stringify(genManualExplicitData)}`);
    }
    console.log(`[PASS] Dynamic Taxonomy: /api/generate preserves explicit manual format when requested`);

    // Test 36: parseSelectionCounts handles 'auto'
    const countsFromAuto = parseSelectionCounts('auto', 3, 1.85, 0.5);
    if (!Array.isArray(countsFromAuto) || countsFromAuto.length === 0) {
      throw new Error(`Expected parseSelectionCounts to handle 'auto' string properly, got: ${JSON.stringify(countsFromAuto)}`);
    }
    console.log(`[PASS] Dynamic Taxonomy: parseSelectionCounts handles 'auto' seamlessly`);

    console.log('--- ALL TESTS PASSED! ---');
  } finally {
    if (serverProcess) {
      serverProcess.kill();
    }
  }
}

runTests().then(() => {
  process.exit(0);
}).catch(err => {
  console.error('[FAIL]', err);
  process.exit(1);
});
