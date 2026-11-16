// CBSE Question Generator Client Application
document.addEventListener('DOMContentLoaded', () => {
  // Lucide Icons initialization
  if (window.lucide) {
    window.lucide.createIcons();
  }

  // Application State
  const state = {
    apiKey: localStorage.getItem('gemini_api_key') || '',
    isMockMode: !localStorage.getItem('gemini_api_key'),
    teacherView: true,
    samples: [],
    currentSampleIndex: 0,
    detectedTopics: [],
    counts: {
      mcq: 2,
      ar: 1,
      vsa: 1,
      sa: 1,
      case: 0,
      la: 1
    },
    questionMode: 'dynamic', // 'dynamic' | 'manual'
    manualTargetQuestions: 25,
    blueprintMode: 'dynamic', // 'dynamic' | 'manual'
    lastGeneratedRaw: ''
  };

  // DOM Elements
  const subjectInput = document.getElementById('subjectInput');
  const gradeInput = document.getElementById('gradeInput');
  const chapterInput = document.getElementById('chapterInput');
  const topicInput = document.getElementById('topicInput');
  const latexChunkInput = document.getElementById('latexChunkInput');
  const chunkCharCount = document.getElementById('chunkCharCount');
  const texFileInput = document.getElementById('texFileInput');
  const uploadTexBtn = document.getElementById('uploadTexBtn');
  const quickSampleBtn = document.getElementById('quickSampleBtn');
  const sectionPickerContainer = document.getElementById('sectionPickerContainer');
  const sectionPicker = document.getElementById('sectionPicker');

  // Question count dual-mode DOM elements
  const modeDynamicBtn = document.getElementById('modeDynamicBtn');
  const modeManualBtn = document.getElementById('modeManualBtn');
  const questionCountBadge = document.getElementById('questionCountBadge');
  const dynamicModeNotice = document.getElementById('dynamicModeNotice');
  const manualModeControls = document.getElementById('manualModeControls');
  const manualQuestionInput = document.getElementById('manualQuestionInput');
  const manualQuestionSlider = document.getElementById('manualQuestionSlider');
  const manualDisplayCount = document.getElementById('manualDisplayCount');

  // Blueprint Category dual-mode DOM elements
  const blueprintModeDynamicBtn = document.getElementById('blueprintModeDynamicBtn');
  const blueprintModeManualBtn = document.getElementById('blueprintModeManualBtn');
  const blueprintModeBadge = document.getElementById('blueprintModeBadge');
  const dynamicBlueprintNotice = document.getElementById('dynamicBlueprintNotice');
  const manualBlueprintSection = document.getElementById('manualBlueprintSection');

  const requestedSelectionDisplay = document.getElementById('requestedSelectionDisplay');
  const totalMarksDisplay = document.getElementById('totalMarksDisplay');
  const totalQuestionsBadge = document.getElementById('totalQuestionsBadge');
  const generateBtn = document.getElementById('generateBtn');

  const emptyState = document.getElementById('emptyState');
  const loadingState = document.getElementById('loadingState');
  const questionsContainer = document.getElementById('questionsContainer');
  const generationModeBadge = document.getElementById('generationModeBadge');

  const modeToggleBtn = document.getElementById('modeToggleBtn');
  const modeDot = document.getElementById('modeDot');
  const modeText = document.getElementById('modeText');

  const apiKeyBtn = document.getElementById('apiKeyBtn');
  const apiKeyModal = document.getElementById('apiKeyModal');
  const closeApiKeyModal = document.getElementById('closeApiKeyModal');
  const geminiApiKeyInput = document.getElementById('geminiApiKeyInput');
  const saveApiKeyBtn = document.getElementById('saveApiKeyBtn');
  const testApiKeyBtn = document.getElementById('testApiKeyBtn');
  const keyStatusAlert = document.getElementById('keyStatusAlert');
  const apiKeyLabel = document.getElementById('apiKeyLabel');

  const viewModeToggle = document.getElementById('viewModeToggle');
  const viewModeText = document.getElementById('viewModeText');
  const printBtn = document.getElementById('printBtn');
  const copyMdBtn = document.getElementById('copyMdBtn');
  const clearSelectionBtn = document.getElementById('clearSelectionBtn');

  // Load Samples on Start
  async function loadSamples() {
    try {
      const res = await fetch('/api/samples');
      const data = await res.json();
      if (data.success && data.samples.length > 0) {
        state.samples = data.samples;
        applySample(state.samples[0]);
      }
    } catch (err) {
      console.warn('Failed to load pre-bundled samples', err);
    }
  }

  function applySample(sample) {
    subjectInput.value = sample.subject;
    gradeInput.value = sample.grade;
    chapterInput.value = sample.chapter_title;
    topicInput.value = sample.topic_name;
    latexChunkInput.value = sample.latex_chunk;
    updateCharCount();
    sectionPickerContainer.classList.add('hidden');
    updateCountersUI();
  }

  // Update Character Count
  function updateCharCount() {
    const len = latexChunkInput.value.length;
    chunkCharCount.textContent = len.toLocaleString();
  }
  latexChunkInput.addEventListener('input', () => {
    updateCharCount();
    updateCountersUI();
  });

  // Cycle Samples
  quickSampleBtn.addEventListener('click', () => {
    if (state.samples.length === 0) return;
    state.currentSampleIndex = (state.currentSampleIndex + 1) % state.samples.length;
    applySample(state.samples[state.currentSampleIndex]);
  });

  // File Upload & Section Splitting
  uploadTexBtn.addEventListener('click', () => texFileInput.click());
  texFileInput.addEventListener('change', async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    const reader = new FileReader();
    reader.onload = async (event) => {
      const text = event.target.result;
      latexChunkInput.value = text;
      updateCharCount();

      // Ask backend to inspect sections
      try {
        const res = await fetch('/api/split-chunk', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ latex: text })
        });
        const data = await res.json();
        if (data.success && data.topics.length > 1) {
          state.detectedTopics = data.topics;
          populateSectionPicker(data.topics);
        } else {
          sectionPickerContainer.classList.add('hidden');
        }
      } catch (err) {
        console.warn('Error splitting chunk:', err);
      }
    };
    reader.readAsText(file);
  });

  function populateSectionPicker(topics) {
    sectionPicker.innerHTML = '';
    topics.forEach((t, idx) => {
      const opt = document.createElement('option');
      opt.value = idx;
      opt.textContent = `[${t.type}] ${t.title}`;
      sectionPicker.appendChild(opt);
    });
    sectionPickerContainer.classList.remove('hidden');

    // On select
    sectionPicker.onchange = () => {
      const chosen = topics[sectionPicker.value];
      if (chosen) {
        topicInput.value = chosen.title;
        latexChunkInput.value = chosen.content;
        updateCharCount();
      }
    };

    // Auto select first
    sectionPicker.selectedIndex = 0;
    sectionPicker.onchange();
  }

  // Mode & API Key Management
  function updateModeUI() {
    if (state.isMockMode) {
      modeDot.className = 'w-2 h-2 rounded-full bg-emerald-400 animate-pulse';
      modeText.textContent = 'Demo / Mock Mode';
      modeToggleBtn.className = 'flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-950 text-emerald-300 border border-emerald-700 hover:bg-emerald-900 transition';
    } else {
      modeDot.className = 'w-2 h-2 rounded-full bg-blue-400';
      modeText.textContent = 'Live Gemini 2.5 Flash';
      modeToggleBtn.className = 'flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-blue-950 text-blue-300 border border-blue-700 hover:bg-blue-900 transition';
    }
  }

  async function checkServerStatus() {
    try {
      const res = await fetch('/api/status');
      const data = await res.json();
      if (data.hasEnvKey) {
        state.hasEnvKey = true;
        if (!localStorage.getItem('gemini_mock_mode_explicit')) {
          state.isMockMode = false;
          apiKeyLabel.textContent = 'Key Active (.env) ✓';
          updateModeUI();
        }
      }
    } catch (e) {
      console.warn('Could not query /api/status', e);
    }
  }

  modeToggleBtn.addEventListener('click', () => {
    if (!state.apiKey && state.isMockMode) {
      openApiKeyModal();
      return;
    }
    state.isMockMode = !state.isMockMode;
    updateModeUI();
  });

  function openApiKeyModal() {
    geminiApiKeyInput.value = state.apiKey;
    keyStatusAlert.className = 'hidden';
    apiKeyModal.classList.remove('hidden');
  }

  apiKeyBtn.addEventListener('click', openApiKeyModal);
  closeApiKeyModal.addEventListener('click', () => apiKeyModal.classList.add('hidden'));

  saveApiKeyBtn.addEventListener('click', () => {
    const key = geminiApiKeyInput.value.trim();
    if (key) {
      state.apiKey = key;
      localStorage.setItem('gemini_api_key', key);
      state.isMockMode = false;
      apiKeyLabel.textContent = 'Key Saved ✓';
      updateModeUI();
      apiKeyModal.classList.add('hidden');
    } else {
      state.apiKey = '';
      localStorage.removeItem('gemini_api_key');
      state.isMockMode = true;
      apiKeyLabel.textContent = 'Gemini API Key';
      updateModeUI();
      apiKeyModal.classList.add('hidden');
    }
  });

  testApiKeyBtn.addEventListener('click', async () => {
    const key = geminiApiKeyInput.value.trim();
    if (!key) {
      showKeyAlert('Please enter an API key first.', 'error');
      return;
    }

    testApiKeyBtn.disabled = true;
    testApiKeyBtn.innerHTML = '<span class="animate-spin">⏳</span> Testing...';

    try {
      const res = await fetch('/api/verify-key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apiKey: key })
      });
      const data = await res.json();
      if (res.ok && data.success) {
        showKeyAlert(`Valid! Model connected: ${data.model}`, 'success');
      } else {
        showKeyAlert(data.error || 'Failed to authenticate key.', 'error');
      }
    } catch (err) {
      showKeyAlert('Error testing key: ' + err.message, 'error');
    } finally {
      testApiKeyBtn.disabled = false;
      testApiKeyBtn.innerHTML = '<i data-lucide="activity" class="w-3.5 h-3.5"></i> Test Key';
      if (window.lucide) window.lucide.createIcons();
    }
  });

  function showKeyAlert(msg, type) {
    keyStatusAlert.textContent = msg;
    keyStatusAlert.className = type === 'success' 
      ? 'p-3 rounded-lg text-xs font-medium bg-emerald-100 text-emerald-800 border border-emerald-300'
      : 'p-3 rounded-lg text-xs font-medium bg-rose-100 text-rose-800 border border-rose-300';
  }

  // Question Counters & Blueprint Selection
  const marksMap = {
    mcq: 1,
    ar: 1,
    vsa: 2,
    sa: 3,
    case: 4,
    la: 5
  };

  const labelMap = {
    mcq: 'Objective MCQ(s)',
    ar: 'Assertion-Reason (1M)',
    vsa: 'Very Short Answer (2M)',
    sa: 'Short Answer (3M)',
    case: 'Case-Based (4M)',
    la: 'Long Answer / Derivation (5M)'
  };

  function updateCountersUI() {
    let totalMarks = 0;
    let totalQuestions = 0;
    const parts = [];

    for (const [key, count] of Object.entries(state.counts)) {
      const el = document.getElementById(`count_${key}`);
      if (el) el.textContent = count;
      totalMarks += count * marksMap[key];
      totalQuestions += count;

      if (count > 0) {
        if (key === 'mcq') parts.push(`${count} MCQ${count > 1 ? 's' : ''}`);
        else if (key === 'ar') parts.push(`${count} Assertion-Reason`);
        else if (key === 'vsa') parts.push(`${count} Very Short Answer (2M)`);
        else if (key === 'sa') parts.push(`${count} Short Answer (3M)`);
        else if (key === 'case') parts.push(`${count} Case-Based (4M)`);
        else if (key === 'la') parts.push(`${count} Long Answer / Derivation (5M)`);
      }
    }

    const hasChunk = !!(latexChunkInput && latexChunkInput.value.trim());

    if (state.blueprintMode === 'dynamic') {
      requestedSelectionDisplay.textContent = '⚡ Auto-allocated based on Topic Weight & Technical Rigor';
      totalMarksDisplay.textContent = 'Auto';
      totalQuestionsBadge.textContent = state.questionMode === 'dynamic' ? '⚡ Dynamic Auto' : `${state.manualTargetQuestions} Qs (Auto)`;
      generateBtn.disabled = !hasChunk;
    } else {
      totalMarksDisplay.textContent = `${totalMarks} M`;
      totalQuestionsBadge.textContent = `${totalQuestions} Question${totalQuestions === 1 ? '' : 's'}`;

      if (parts.length > 0) {
        requestedSelectionDisplay.textContent = parts.join(', ');
        generateBtn.disabled = !hasChunk;
      } else {
        requestedSelectionDisplay.textContent = 'Select at least one question type above';
        generateBtn.disabled = true;
      }
    }
  }

  // Stepper Buttons
  document.querySelectorAll('.counter-btn-inc').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.target;
      state.counts[target] = (state.counts[target] || 0) + 1;
      updateCountersUI();
    });
  });

  document.querySelectorAll('.counter-btn-dec').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.target;
      if (state.counts[target] > 0) {
        state.counts[target]--;
        updateCountersUI();
      }
    });
  });

  // Presets
  const presets = {
    standard: { mcq: 2, ar: 1, vsa: 1, sa: 1, case: 0, la: 1 }, // 2 + 1 + 2 + 3 + 5 = 13M
    numerical_derivation: { mcq: 0, ar: 0, vsa: 1, sa: 2, case: 0, la: 2 }, // 2 + 6 + 10 = 18M
    case_study: { mcq: 0, ar: 0, vsa: 0, sa: 0, case: 2, la: 0 }, // 8M
    objective: { mcq: 4, ar: 2, vsa: 0, sa: 0, case: 0, la: 0 } // 6M
  };

  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const p = presets[btn.dataset.preset];
      if (p) {
        state.counts = { ...p };
        updateCountersUI();
      }
    });
  });

  clearSelectionBtn.addEventListener('click', () => {
    for (const k in state.counts) state.counts[k] = 0;
    updateCountersUI();
  });

  // View Mode: Student vs Teacher
  viewModeToggle.addEventListener('click', () => {
    state.teacherView = !state.teacherView;
    if (state.teacherView) {
      document.body.classList.remove('student-mode');
      viewModeText.textContent = 'Teacher View (Solutions Visible)';
      viewModeToggle.classList.remove('bg-amber-100', 'text-amber-900', 'border-amber-300');
      viewModeToggle.classList.add('bg-slate-100', 'text-slate-800');
    } else {
      document.body.classList.add('student-mode');
      viewModeText.textContent = 'Student View (Questions Only)';
      viewModeToggle.classList.remove('bg-slate-100', 'text-slate-800');
      viewModeToggle.classList.add('bg-amber-100', 'text-amber-900', 'border-amber-300');
    }
  });

  // Print Exam
  printBtn.addEventListener('click', () => {
    document.getElementById('printExamTitle').textContent = `${gradeInput.value ? 'Class ' + gradeInput.value : 'Senior Secondary'} Board Examination`;
    document.getElementById('printSubjectGrade').textContent = `Subject: ${subjectInput.value} | Class ${gradeInput.value}`;
    document.getElementById('printTopic').textContent = `Topic: ${topicInput.value}`;
    document.getElementById('printTotalMarks').textContent = `Maximum Marks: ${totalMarksDisplay.textContent}`;
    window.print();
  });

  // Copy Markdown
  copyMdBtn.addEventListener('click', () => {
    if (!state.lastGeneratedRaw) return;
    navigator.clipboard.writeText(state.lastGeneratedRaw).then(() => {
      const orig = copyMdBtn.innerHTML;
      copyMdBtn.innerHTML = '<i data-lucide="check" class="w-3.5 h-3.5 text-emerald-600"></i> Copied!';
      if (window.lucide) window.lucide.createIcons();
      setTimeout(() => {
        copyMdBtn.innerHTML = orig;
        if (window.lucide) window.lucide.createIcons();
      }, 2000);
    });
  });

  // Question Count Dual-Mode Switcher
  function setQuestionCountMode(mode) {
    state.questionMode = mode;
    if (mode === 'dynamic') {
      if (modeDynamicBtn) modeDynamicBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border-2 border-indigo-600 bg-indigo-600 text-white shadow-sm flex items-center justify-center space-x-1.5 transition';
      if (modeManualBtn) modeManualBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white hover:bg-slate-100 text-slate-700 flex items-center justify-center space-x-1.5 transition';
      if (questionCountBadge) {
        questionCountBadge.textContent = 'Auto (Dynamic)';
        questionCountBadge.className = 'text-[11px] px-2 py-0.5 rounded-full font-bold bg-indigo-100 text-indigo-800 border border-indigo-200';
      }
      if (dynamicModeNotice) dynamicModeNotice.classList.remove('hidden');
      if (manualModeControls) manualModeControls.classList.add('hidden');
    } else {
      if (modeManualBtn) modeManualBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border-2 border-indigo-600 bg-indigo-600 text-white shadow-sm flex items-center justify-center space-x-1.5 transition';
      if (modeDynamicBtn) modeDynamicBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white hover:bg-slate-100 text-slate-700 flex items-center justify-center space-x-1.5 transition';
      if (questionCountBadge) {
        questionCountBadge.textContent = `Manual: ${state.manualTargetQuestions} Qs`;
        questionCountBadge.className = 'text-[11px] px-2 py-0.5 rounded-full font-bold bg-amber-100 text-amber-900 border border-amber-300';
      }
      if (dynamicModeNotice) dynamicModeNotice.classList.add('hidden');
      if (manualModeControls) manualModeControls.classList.remove('hidden');
    }
  }

  function updateManualTarget(val) {
    let num = parseInt(val, 10);
    if (isNaN(num)) num = 25;
    num = Math.max(10, Math.min(50, num));
    state.manualTargetQuestions = num;
    if (manualQuestionInput && document.activeElement !== manualQuestionInput) manualQuestionInput.value = num;
    if (manualQuestionSlider) manualQuestionSlider.value = num;
    if (manualDisplayCount) manualDisplayCount.textContent = num;
    if (state.questionMode === 'manual' && questionCountBadge) {
      questionCountBadge.textContent = `Manual: ${num} Qs`;
    }

    // Highlight matching quick preset if any
    document.querySelectorAll('.quick-target-btn').forEach(btn => {
      const targetVal = parseInt(btn.dataset.target, 10);
      if (targetVal === num) {
        btn.className = 'quick-target-btn px-2 py-0.5 text-[10px] font-semibold rounded bg-indigo-50 border border-indigo-300 text-indigo-700 transition';
      } else {
        btn.className = 'quick-target-btn px-2 py-0.5 text-[10px] font-semibold rounded bg-slate-100 hover:bg-indigo-100 hover:text-indigo-800 border border-slate-200 text-slate-700 transition';
      }
    });
  }

  if (modeDynamicBtn) modeDynamicBtn.addEventListener('click', () => setQuestionCountMode('dynamic'));
  if (modeManualBtn) modeManualBtn.addEventListener('click', () => setQuestionCountMode('manual'));

  if (manualQuestionInput) {
    manualQuestionInput.addEventListener('input', (e) => {
      const val = parseInt(e.target.value, 10);
      if (!isNaN(val)) {
        if (manualQuestionSlider) manualQuestionSlider.value = Math.max(10, Math.min(50, val));
        if (manualDisplayCount) manualDisplayCount.textContent = Math.max(10, Math.min(50, val));
        if (state.questionMode === 'manual' && questionCountBadge) {
          questionCountBadge.textContent = `Manual: ${Math.max(10, Math.min(50, val))} Qs`;
        }
      }
    });
    manualQuestionInput.addEventListener('change', (e) => updateManualTarget(e.target.value));
    manualQuestionInput.addEventListener('blur', (e) => updateManualTarget(e.target.value));
  }
  if (manualQuestionSlider) {
    manualQuestionSlider.addEventListener('input', (e) => updateManualTarget(e.target.value));
  }

  // Quick preset total buttons
  document.querySelectorAll('.quick-target-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const val = parseInt(btn.dataset.target, 10);
      if (!isNaN(val)) {
        setQuestionCountMode('manual');
        updateManualTarget(val);
      }
    });
  });

  // Blueprint Category Dual-Mode Switcher
  function setBlueprintMode(mode) {
    state.blueprintMode = mode;
    if (mode === 'dynamic') {
      if (blueprintModeDynamicBtn) blueprintModeDynamicBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border-2 border-indigo-600 bg-indigo-600 text-white shadow-sm flex items-center justify-center space-x-1.5 transition';
      if (blueprintModeManualBtn) blueprintModeManualBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white hover:bg-slate-100 text-slate-700 flex items-center justify-center space-x-1.5 transition';
      if (blueprintModeBadge) {
        blueprintModeBadge.textContent = 'Auto (Dynamic)';
        blueprintModeBadge.className = 'text-[11px] px-2 py-0.5 rounded-full font-bold bg-indigo-100 text-indigo-800 border border-indigo-200';
      }
      if (dynamicBlueprintNotice) dynamicBlueprintNotice.classList.remove('hidden');
      if (manualBlueprintSection) manualBlueprintSection.classList.add('hidden');
    } else {
      if (blueprintModeManualBtn) blueprintModeManualBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border-2 border-indigo-600 bg-indigo-600 text-white shadow-sm flex items-center justify-center space-x-1.5 transition';
      if (blueprintModeDynamicBtn) blueprintModeDynamicBtn.className = 'px-2.5 py-1.5 text-xs font-semibold rounded-lg border border-slate-300 bg-white hover:bg-slate-100 text-slate-700 flex items-center justify-center space-x-1.5 transition';
      if (blueprintModeBadge) {
        blueprintModeBadge.textContent = 'Custom Format';
        blueprintModeBadge.className = 'text-[11px] px-2 py-0.5 rounded-full font-bold bg-amber-100 text-amber-900 border border-amber-300';
      }
      if (dynamicBlueprintNotice) dynamicBlueprintNotice.classList.add('hidden');
      if (manualBlueprintSection) manualBlueprintSection.classList.remove('hidden');
    }
    updateCountersUI();
  }

  if (blueprintModeDynamicBtn) blueprintModeDynamicBtn.addEventListener('click', () => setBlueprintMode('dynamic'));
  if (blueprintModeManualBtn) blueprintModeManualBtn.addEventListener('click', () => setBlueprintMode('manual'));

  // Generation Handler
  generateBtn.addEventListener('click', async () => {
    const chunk = latexChunkInput.value.trim();
    if (!chunk) {
      alert('Please provide a LaTeX chunk first.');
      return;
    }

    let selection = 'auto';
    if (state.blueprintMode === 'manual') {
      const manualSelection = requestedSelectionDisplay.textContent;
      if (!manualSelection || manualSelection.includes('Select at least one')) {
        alert('Please select at least one question type in Custom Format, or switch to Auto by Weight & Rigor.');
        return;
      }
      selection = manualSelection;
    }

    // Update UI states
    emptyState.classList.add('hidden');
    questionsContainer.classList.add('hidden');
    loadingState.classList.remove('hidden');
    generateBtn.disabled = true;

    try {
      const payload = {
        subject: subjectInput.value.trim(),
        grade: gradeInput.value.trim(),
        chapter_title: chapterInput.value.trim(),
        topic_name: topicInput.value.trim(),
        latex_chunk: chunk,
        requested_selection: selection,
        apiKey: state.apiKey,
        useMock: state.isMockMode,
        target_questions: state.questionMode === 'dynamic' ? 'auto' : state.manualTargetQuestions,
        dynamic_target: state.questionMode === 'dynamic',
        validate_range: true,
      };

      const res = await fetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const data = await res.json();
      if (!res.ok) {
        throw new Error(data.error || 'Failed to generate questions');
      }

      state.lastGeneratedRaw = data.output;
      renderQuestions(data.output, data.mode);

    } catch (err) {
      alert('Generation Failed: ' + err.message);
      emptyState.classList.remove('hidden');
    } finally {
      loadingState.classList.add('hidden');
      generateBtn.disabled = false;
    }
  });

  // Parser & Renderer for Generated Questions
  function renderQuestions(rawText, mode) {
    questionsContainer.innerHTML = '';
    questionsContainer.classList.remove('hidden');

    // Badge for generation mode
    if (mode === 'live') {
      generationModeBadge.textContent = 'Live Gemini 2.5 Flash';
      generationModeBadge.className = 'text-[10px] px-2 py-0.5 rounded font-bold uppercase tracking-wider bg-blue-100 text-blue-800 border border-blue-300';
    } else {
      generationModeBadge.textContent = 'Simulated / Mock Mode';
      generationModeBadge.className = 'text-[10px] px-2 py-0.5 rounded font-bold uppercase tracking-wider bg-emerald-100 text-emerald-800 border border-emerald-300';
    }
    generationModeBadge.classList.remove('hidden');

    // Split text into question sections using ### Q or --- delimiters
    const questionBlocks = rawText.split(/(?=###\s*Q\d+)/g).filter(b => b.trim().length > 0);

    if (questionBlocks.length === 0) {
      // Fallback display if not split
      const fallbackCard = document.createElement('div');
      fallbackCard.className = 'p-5 bg-white rounded-xl border border-slate-200 text-sm whitespace-pre-wrap';
      fallbackCard.textContent = rawText;
      questionsContainer.appendChild(fallbackCard);
      renderKaTeX(questionsContainer);
      return;
    }

    questionBlocks.forEach((block, index) => {
      const card = createQuestionCard(block, index + 1);
      questionsContainer.appendChild(card);
    });

    // Render KaTeX for formulas
    renderKaTeX(questionsContainer);
    if (window.lucide) window.lucide.createIcons();
  }

  function createQuestionCard(blockText, fallbackNum) {
    const card = document.createElement('div');
    card.className = 'question-card bg-white rounded-xl border border-slate-200 shadow-sm overflow-hidden p-5 flex flex-col space-y-4';

    // Parse header: e.g. ### Q1 | Objective — MCQ | 1 Mark(s) | 🔴 Tier 1
    const firstLine = blockText.trim().split('\n')[0].replace(/^###\s*/, '').trim();
    const headerParts = firstLine.split('|').map(s => s.trim());
    
    let qLabel = `Q${fallbackNum}`;
    let category = 'CBSE Question';
    let marks = '1 Mark';
    let tier = '🔴 Tier 1';

    if (headerParts.length >= 1 && headerParts[0]) qLabel = headerParts[0];
    if (headerParts.length >= 2 && headerParts[1]) category = headerParts[1];
    if (headerParts.length >= 3 && headerParts[2]) marks = headerParts[2];
    if (headerParts.length >= 4 && headerParts[3]) tier = headerParts[3];

    const isTier2 = tier.includes('Tier 2') || tier.includes('🟡');
    const tierBadgeClass = isTier2 ? 'badge-tier-2' : 'badge-tier-1';
    const tierIcon = isTier2 ? '🟡' : '🔴';

    // Target Concept
    const conceptMatch = blockText.match(/\*\*Target Concept:\*\*\s*([^\n]+)/i);
    const targetConcept = conceptMatch ? conceptMatch[1].trim() : '';

    // Why this Tier
    const tierWhyMatch = blockText.match(/\*\*Why this Tier:\*\*\s*([^\n]+)/i);
    const whyTier = tierWhyMatch ? tierWhyMatch[1].trim() : '';

    // Extract Question text and Marking Scheme
    let questionText = '';
    let markingSchemeText = '';

    const markingIndex = blockText.indexOf('**Marking Scheme & Solution:**');
    if (markingIndex !== -1) {
      const questionPart = blockText.substring(0, markingIndex);
      markingSchemeText = blockText.substring(markingIndex + '**Marking Scheme & Solution:**'.length);

      // Clean up question part
      const qStart = questionPart.indexOf('**Question:**');
      if (qStart !== -1) {
        questionText = questionPart.substring(qStart + '**Question:**'.length).trim();
      } else {
        questionText = questionPart.replace(/###[^\n]+\n/, '').trim();
      }

      // Clean up marking scheme text (cut before Why this Tier)
      const whyIndex = markingSchemeText.indexOf('**Why this Tier:**');
      if (whyIndex !== -1) {
        markingSchemeText = markingSchemeText.substring(0, whyIndex).trim();
      }
    } else {
      // Fallback
      questionText = blockText.replace(/###[^\n]+\n/, '').trim();
    }

    // Check for Diagram Setup
    let diagramSetupText = '';
    const diagMatch = questionText.match(/Diagram Setup:\s*([^\n]+)/i);
    if (diagMatch) {
      diagramSetupText = diagMatch[1].trim();
      questionText = questionText.replace(/Diagram Setup:\s*[^\n]+/i, '').trim();
    }

    // Check for [SKIPPED: ...]
    const isSkipped = blockText.includes('[SKIPPED:');

    // Build Card HTML
    card.innerHTML = `
      <!-- Card Header -->
      <div class="flex flex-wrap items-center justify-between gap-2 pb-3 border-b border-slate-100">
        <div class="flex items-center space-x-2">
          <span class="w-8 h-8 rounded-lg bg-indigo-900 text-white font-bold flex items-center justify-center text-xs shadow-sm">
            ${qLabel}
          </span>
          <div>
            <div class="flex items-center gap-2">
              <span class="text-sm font-bold text-slate-800">${escapeHtml(category)}</span>
              <span class="text-xs px-2 py-0.5 rounded-full font-bold bg-indigo-50 text-indigo-700 border border-indigo-200">
                ${escapeHtml(marks)}
              </span>
            </div>
            ${targetConcept ? `<p class="text-[11px] text-slate-500 font-medium">Concept: ${escapeHtml(targetConcept)}</p>` : ''}
          </div>
        </div>

        <div class="flex items-center space-x-2">
          <span class="text-xs px-2.5 py-1 rounded-md font-bold ${tierBadgeClass} flex items-center gap-1 shadow-sm">
            ${escapeHtml(tier)}
          </span>
        </div>
      </div>

      ${isSkipped ? `
        <div class="p-3 bg-amber-50 border border-amber-200 rounded-lg text-amber-800 text-xs font-medium">
          ${escapeHtml(blockText.match(/\[SKIPPED:[^\]]+\]/)?.[0] || 'Skipped format')}
        </div>
      ` : `
        <!-- Diagram Setup if any -->
        ${diagramSetupText ? `
          <div class="diagram-box">
            <div class="font-bold flex items-center gap-1 mb-1 text-indigo-900">
              <i data-lucide="image" class="w-3.5 h-3.5"></i> Diagram Setup / Visual Description
            </div>
            <div>${formatMarkdownInline(diagramSetupText)}</div>
          </div>
        ` : ''}

        <!-- Question Body -->
        <div class="text-sm text-slate-800 font-normal leading-relaxed pl-1 space-y-2">
          ${formatQuestionBody(questionText, category)}
        </div>

        <!-- Marking Scheme & Solution (Collapsible) -->
        <div class="marking-scheme-container pt-3 border-t border-slate-100">
          <div class="bg-indigo-50/70 border border-indigo-100 rounded-xl overflow-hidden transition">
            <button class="w-full px-4 py-2.5 bg-indigo-100/60 hover:bg-indigo-100/90 text-left flex items-center justify-between transition group marking-toggle-btn">
              <div class="flex items-center space-x-2">
                <i data-lucide="check-circle-2" class="w-4 h-4 text-indigo-700"></i>
                <span class="text-xs font-bold text-indigo-950 uppercase tracking-wider">Step-Wise Marking Scheme & NCERT Solution</span>
              </div>
              <span class="text-xs text-indigo-600 font-semibold group-hover:text-indigo-800 toggle-arrow">▼</span>
            </button>
            <div class="p-4 text-xs text-slate-700 space-y-2 marking-content">
              ${formatMarkingScheme(markingSchemeText)}
            </div>
          </div>
        </div>

        <!-- Why this Tier -->
        ${whyTier ? `
          <div class="tier-rationale flex items-center space-x-2 text-[11px] text-slate-500 pt-1 border-t border-slate-50">
            <span class="font-bold text-slate-700">Exam Context:</span>
            <span>${formatMarkdownInline(whyTier)}</span>
          </div>
        ` : ''}
      `}
    `;

    // Collapsible marking scheme toggle
    const toggleBtn = card.querySelector('.marking-toggle-btn');
    const markingContent = card.querySelector('.marking-content');
    const toggleArrow = card.querySelector('.toggle-arrow');
    if (toggleBtn && markingContent) {
      toggleBtn.addEventListener('click', () => {
        markingContent.classList.toggle('hidden');
        toggleArrow.textContent = markingContent.classList.contains('hidden') ? '▲' : '▼';
      });
    }

    return card;
  }

  // Formatting helpers
  function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function formatMarkdownInline(text) {
    if (!text) return '';
    // Format bold **text**
    let formatted = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    return formatted;
  }

  function formatQuestionBody(text, category = '') {
    if (!text) return '';
    const isCase = category.toLowerCase().includes('case') || category.toLowerCase().includes('source');
    const lines = text.split('\n');
    let html = '';
    let inList = false;

    lines.forEach(line => {
      const trimmed = line.trim();
      if (!trimmed) {
        if (inList) { html += '</div>'; inList = false; }
        return;
      }

      // If Case study, subparts like (a), (b), (c) should be rendered as distinct sub-questions
      if (isCase && /^\([a-c]\)\s+/i.test(trimmed)) {
        if (inList) { html += '</div>'; inList = false; }
        html += `<div class="p-3 bg-slate-50/90 rounded-lg border border-slate-200 my-2 font-medium text-xs text-slate-800 pl-3.5 border-l-4 border-l-purple-500">${formatMarkdownInline(trimmed)}</div>`;
        return;
      }

      // MCQ / A-R option: (A) ... or (a) ...
      if (/^\([A-Da-d]\)\s+/.test(trimmed)) {
        if (!inList) {
          html += '<div class="grid grid-cols-1 sm:grid-cols-2 gap-2 my-2.5 font-medium">';
          inList = true;
        }
        const optLetter = trimmed.match(/^\(([A-Da-d])\)/)[1];
        const optContent = trimmed.replace(/^\([A-Da-d]\)\s+/, '');
        html += `
          <div class="flex items-start space-x-2 p-2 rounded-lg bg-slate-50 border border-slate-200">
            <span class="font-bold text-indigo-700 text-xs">(${optLetter})</span>
            <span class="text-xs text-slate-800">${formatMarkdownInline(optContent)}</span>
          </div>
        `;
      } else {
        if (inList) { html += '</div>'; inList = false; }
        if (/^\*\*(OR|Case Study Passage:)\*\*/i.test(trimmed)) {
          html += `<div class="font-bold text-xs my-2 text-indigo-900 bg-indigo-50/60 p-2 rounded border border-indigo-100">${formatMarkdownInline(trimmed)}</div>`;
        } else {
          html += `<p class="my-1.5">${formatMarkdownInline(trimmed)}</p>`;
        }
      }
    });

    if (inList) html += '</div>';
    return html;
  }

  function formatMarkingScheme(text) {
    if (!text) return '<em>Detailed step-wise marking scheme provided in official CBSE key.</em>';
    const lines = text.split('\n').filter(l => l.trim().length > 0);
    let html = '<ul class="space-y-2 list-none">';

    lines.forEach(line => {
      let cleaned = line.replace(/^\s*[-•]\s*/, '').trim();
      // Format mark allocations like — **[1/2 M]** or — [1 M]
      cleaned = cleaned.replace(/—\s*\*{0,2}\[([^\]]+)\]\*{0,2}/g, '<span class="mark-allocation">$1</span>');
      cleaned = formatMarkdownInline(cleaned);
      html += `<li class="flex items-start space-x-2"><span class="text-indigo-500 font-bold mt-0.5">•</span><span class="leading-relaxed">${cleaned}</span></li>`;
    });

    html += '</ul>';
    return html;
  }

  // KaTeX Auto-Render Trigger
  function renderKaTeX(domElement) {
    if (window.renderMathInElement) {
      try {
        window.renderMathInElement(domElement, {
          delimiters: [
            { left: '$$', right: '$$', display: true },
            { left: '$', right: '$', display: false },
            { left: '\\[', right: '\\]', display: true },
            { left: '\\(', right: '\\)', display: false }
          ],
          throwOnError: false
        });
      } catch (e) {
        console.error('KaTeX rendering error:', e);
      }
    }
  }

  // Initial State Setup
  updateModeUI();
  setQuestionCountMode('dynamic');
  setBlueprintMode('dynamic');
  loadSamples();
  checkServerStatus();
});
