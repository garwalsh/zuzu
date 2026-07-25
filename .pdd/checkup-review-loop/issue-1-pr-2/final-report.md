## Step 7/8: Review Loop Final Report

PR: https://github.com/garwalsh/zuzu/pull/2
Issue: https://github.com/garwalsh/zuzu/issues/1
issue_aligned: unknown
active-reviewer: claude
same-role-review-fix: true
role-independence: independent
reviewer-status: claude=failed fresh-final=missing
fresh-final-review: missing
verified-head-sha: none
remote-pr-head-sha: 00abe924b95615c788715ee4713897a4ba4b9a6d
test-scope: full
full-suite-source: local
max-rounds-reached: false
max-cost-reached: false
max-duration-reached: false

### Summary

Primary reviewer claude could not verify fixes: failed.

Verification scope: local full suite plus Layer 2 review-loop.

### Per-Reviewer Status

| Reviewer | Status |
|----------|--------|
| claude | failed |
| fresh-final | missing |

### Machine Verdict

```json
{
  "active_reviewer": "claude",
  "failure_category": "review_findings_remain",
  "findings": [
    {
      "area": "test",
      "evidence": "status: failed\ncommand: python -m pytest -q tests/test_contract.py tests/test_schema_matches_pdf.py\nexit_code: 1\nselected_tests: tests/test_contract.py, tests/test_schema_matches_pdf.py\nartifact_path: .pdd/checkup-pr-2/layer1-step5-evidence.json\noutput:\nEEEEEEEEEEEEEEFFEEEEEEEEEEEEEEE.........                                 [100%]\n==================================== ERRORS ====================================\n_________________ ERROR at setup of test_health_needs_no_auth __________________\n\n    @pytest.fixture\n    def client():\n>       from api.main import app\n\ntests/test_contract.py:39: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \napi/main.py:47: in <module>\n    from api.pdf_engine import fill_i765, missing_required\n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n\n    \"\"\"Fill the official USCIS I-765 AcroForm.\n    \n    Spec: prompts/pdf_engine_Python.prompt\n    \n    Every non-obvious step below exists because the obvious version fails silently:\n    it reports success and hands the applicant a blank form.\n    \"\"\"\n    \n    from __future__ import annotations\n    \n    import logging\n    import re\n    from pathlib import Path\n    \n>   from pypdf import PdfReader, PdfWriter\nE   ModuleNotFoundError: No module named 'pypdf'\n\napi/pdf_engine.py:15: ModuleNotFoundError\n_________ ERROR at setup of test_full_call_produces_a_downloadable_pdf _________\n\n    @pytest.fixture\n    def client():\n>       from api.main import app\n\ntests/test_contract.py:39: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \napi/main.py:47: in <module>\n    from api.pdf_engine import fill_i765, missing_required\n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n\n    \"\"\"Fill the official USCIS I-765 AcroForm.\n    \n    Spec: prompts/pdf_engine_Python.prompt\n    \n    Every non-obvious step below exists because the obvious version fails silently:\n    it reports success and hands the applicant a blank form.\n    \"\"\"\n    \n    from __future__ import annotations\n    \n    import logging\n    import re\n    from pathlib import Path\n    \n>   from pypdf import PdfReader, PdfWriter\nE   ModuleNotFoundError: No module named 'pypdf'\n\napi/pdf_engine.py:15: ModuleNotFoundError\n________ ERROR at setup of test_interview_terminates_and_counts_go_down ________\n\n    @pytest.fixture\n    def client():\n>       from api.main import app\n\ntests/test_contract.py:39: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \napi/main.py:47: in <module>\n    from api.pdf_engine import fill_i765, missing_required\n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n\n    \"\"\"Fill the official USCIS I-765 AcroForm.\n    \n    Spec: prompts/pdf_engine_Python.prompt\n    \n    Every non-obvious step below exists because the obvious version fails silently:\n    it reports success and hands the applicant a blank form.\n    \"\"\"\n    \n    from __future__ import annotations\n    \n    import logging\n    import re\n    from pathlib import Path\n    \n>   from pypdf import PdfReader, PdfWriter\nE   ModuleNotFoundError: No module named 'pypdf'\n\napi/pdf_engine.py:15: ModuleNotFoundError\n___________ ERROR at setup of test_form_id_spelling_variants_resolve ___________\n\n    @pytest.fixture\n    def client():\n>       from api.main import app\n\ntests/test_contract.py:39: \n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \napi/main.py:47: in <module>\n    from api.pdf_engine import fill_i765, missing_required\n_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ \n\n    \"\"\"Fill the official USCIS I-765 AcroForm.\n    \n    Spec: prompts/pdf_engine_Python.prompt\n    \n    Every non-obvious step below exists because the obvious version fails silently:\n    it reports success and hands the applicant a blank form.\n    \"\"\"\n    \n    from __future__ import annotations\n    \n    import logging\n    import re\n    from p",
      "finding": "Layer 1 Step 5 shell-first test execution failed before Layer 2.",
      "key": "|critical|tests/test_contract.py|layer 1 step 5 shell-first test execution failed before layer 2.|fix the code or tests causing this command to fail, then rerun `python -m pytest -q tests/test_contract.py tests/test_schema_matches_pdf.py` or an equivalent targeted check.",
      "location": "tests/test_contract.py",
      "required_fix": "Fix the code or tests causing this command to fail, then rerun `python -m pytest -q tests/test_contract.py tests/test_schema_matches_pdf.py` or an equivalent targeted check.",
      "reviewer": "layer1:step5",
      "round": "0",
      "severity": "critical",
      "status": "open"
    }
  ],
  "fresh_final_status": "missing",
  "full_suite_source": "local",
  "github_ci_gate_used": false,
  "issue_aligned": null,
  "issue_url": "https://github.com/garwalsh/zuzu/issues/1",
  "max_cost_reached": false,
  "max_duration_reached": false,
  "max_rounds": 1,
  "max_rounds_reached": false,
  "pr_url": "https://github.com/garwalsh/zuzu/pull/2",
  "reason": "Primary reviewer claude could not verify fixes: failed.",
  "remote_pr_head_sha": "00abe924b95615c788715ee4713897a4ba4b9a6d",
  "reviewer_status": {
    "claude": "failed",
    "fresh-final": "missing"
  },
  "rounds_completed": 1,
  "schema": "pdd.checkup.final_gate.v1",
  "sol_model": null,
  "sol_review_status": null,
  "source_of_truth": null,
  "stage": "review-loop",
  "status": "failed",
  "terra_fixer": null,
  "terra_model": null,
  "terra_sol_mode": false,
  "test_scope": "full",
  "verified_head_sha": "none"
}
```

### Reviewer Diagnostics

- claude (failed): classification=unknown, exit code: no exit code

```
Filesystem policy violation: changed files must stay inside caller-declared writable roots and outside declared read-only roots.
Changed files outside writable roots: /Users/bhargav/Desktop/zuzu/.git/worktrees/checkup-pr-2
symlink targets outside audited roots: .venv/bin/python -> /Users/bhargav/.local/share/uv/python/cpython-3.12.8-macos-aarch64-none/bin/python3.12, .venv/bin/python3 -> /Users/bhargav/.local/share/uv/python/cpython-3.12.8-macos-aarch64-none/bin/python3.12, .venv/bin/python3.12 -> /Users/bhargav/.local/share/uv/python/cpython-3.12.8-macos-aarch64-none/bin/python3.12
Changed files: /Users/bhargav/Desktop/zuzu/.git/worktrees/checkup-pr-2, .venv/bin/python, .venv/bin/python3, .venv/bin/python3.12
```


### Findings

| Severity | Status | Location | Finding | Required fix | Reviewer |
|----------|--------|----------|---------|--------------|----------|
| critical | open | tests/test_contract.py | Layer 1 Step 5 shell-first test execution failed before Layer 2. | Fix the code or tests causing this command to fail, then rerun `python -m pytest -q tests/test_contract.py tests/test_schema_matches_pdf.py` or an equivalent targeted check. | layer1:step5 |

### Fixer Rationale

- tests/test_contract.py: Layer 1 Step 5 shell-first test execution failed before Layer 2. (fixer=claude fixer_disposition=fixed fixer_rationale="Primary cause was pypdf not installed in the ad-hoc test env; pypdf is properly declared in pyproject.toml and locked, so uv sync installs it (as CI does). Additionally fixed a latent non-hermetic test bug: _isolate now unsets MEM0_API_KEY and resets the memory singleton so contract tests no longer depend on the developer's shell hitting the live mem0 API. Targeted command `python -m pytest -q tests/test_contract.py tests/test_schema_matches_pdf.py` now passes 40/40."; verification=unverified)

### Fixes Attempted

- round=1 fixer=claude fixer_result=attempted push_status=pushed local_sha=00abe92 pushed_sha=00abe92 changed_files=tests/test_contract.py, .pdd/agentic-logs/session_20260725_162130.jsonl verification=unverified summary=Ran uv sync to install the declared pypdf dependency (was missing from the reviewer's ad-hoc python env), which cleared all ModuleNotFoundError errors. Then fixed a real test-isolation defect: the _isolate fixture in tests/test_contract.py did not neutralize the memory layer, so an ambient MEM0_API_KEY caused session_init to hit the live mem0 API and prefill fields, breaking the fresh-session contract tests. Made the fixture hermetic by delenv-ing MEM0_API_KEY and calling memory.reset_memory() around each test. Full suite now passes (40 passed) even with MEM0_API_KEY exported.