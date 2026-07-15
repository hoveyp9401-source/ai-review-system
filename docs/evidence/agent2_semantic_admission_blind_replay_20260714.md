# Agent2 Semantic Admission Blind Replay Evidence — 2026-07-14

## Scope and evidence class

This is one complete, label-free, 26-case Semantic Admission replay. The runner loaded only raw text, trusted scope, legal conversation state, and trusted resources. It invoked the configured `SemanticInterpreter` and then `DomainAdmissionEngine`. It had no label/scorer path or CLI argument.

The sealed labels are **machine candidates pending human review**. They are not independent adjudicated Gold, so this replay is not acceptance-eligible and cannot establish a production Go decision.

## Physical split and seals

- Blind input: `evals/agent2/semantic_admission/blind_input.json`
- Input digest: `d79f06f4febc8a836aada5f9663c95f08349bb2a8f655e864472c77330a1746d`
- Sealed labels: `evals/agent2/semantic_admission/sealed_labels.json`
- Label seal: `bae9a38c05ed2df4dcee5ffbeef7218890815765c54ca74adee68350ff939fbb`
- Completed actual: `evals/agent2/semantic_admission/actual.json`
- Actual artifact hash: `b54efbef0708ab61781324104016a6beb0b045aaa60821934adcb039a0f39c8f`
- Runtime version hash: `e171801f55f55fa96f2d0b5a6cd583c0ec8e5aa44e593c6daec23f4dc3a7a720`
- Score: `evals/agent2/semantic_admission/score.json`

The actual was created once. No alternative nondeterministic run was selected. Changing and resealing labels changes scoring only; the completed actual and its hash remain unchanged. The scorer rejects incomplete/tampered actuals, invalid label seals, cross-pack labels, and coverage drift.

## Corpus coverage

The 26 synthetic adversarial cases cover:

- old-date daily report text;
- “没其他风险”;
- weekly entry and daily-context hijack;
- generic case material;
- unique case alias and case number;
- ambiguous case alias;
- asserted, incomplete, negated, hypothetical, and quoted travel;
- negated, hypothetical, and quoted case progress;
- two- and three-domain messages;
- valid siblings beside an ambiguous case segment.

## Reproducible commands

```powershell
venv\Scripts\python.exe scripts\build_agent2_semantic_admission_blind_pack.py --output-dir evals\agent2\semantic_admission
venv\Scripts\python.exe scripts\run_agent2_semantic_admission_blind.py --blind-input evals\agent2\semantic_admission\blind_input.json --actual-output evals\agent2\semantic_admission\actual.json --concurrency 4 --timeout-seconds 60 --max-retries 1
venv\Scripts\python.exe scripts\score_agent2_semantic_admission_blind.py --actual evals\agent2\semantic_admission\actual.json --sealed-labels evals\agent2\semantic_admission\sealed_labels.json --output evals\agent2\semantic_admission\score.json
venv\Scripts\python.exe -m pytest tests\test_agent2_semantic_admission_blind.py -q
```

## Result

- Cases: 26
- Machine-candidate matched cases: 8
- Machine-candidate mismatch assertions: 30
- Independent metrics available: `false`
- Acceptance eligible: `false`
- Human review state: `pending_human_review`

## Safety-relevant findings

The sealed actual contains three direct safety blockers against Enforce/Go until diagnosed and fixed:

1. `old_date_daily`: a request explicitly referring to yesterday produced an admitted current daily-report mutation ticket.
2. `travel_negated`: “明天不去南京出差了” produced an admitted travel registration ticket.
3. `case_negated`: “云璟府案今天没有联系法院” produced an admitted case-progress ticket.

Other material mismatches include:

- weekly entry/continuation produced no Admission decision;
- exact case-number progress was blocked by segment grounding;
- asserted travel and several multi-domain siblings were blocked by evidence grounding;
- ambiguous-case Selection request was not produced in the standalone sample;
- “没其他风险” became a case query decision;
- quoted case/travel statements were safely blocked, but still differed from the no-action machine candidate;
- multi-intent sibling preservation was inconsistent when case/travel evidence spans were rejected.

These findings are evidence from the completed actual artifact, not a claim that the pending machine labels are independent Gold. The three admitted negated/old-date writes are fail-safe blockers even before independent label adjudication because they conflict with the explicit Phase 1 safety contract.

## Frozen deployed-runtime run

After all code reviews and the Shadow deployment, one new label-free run was started on the server-staged copy of the exact deployed Runtime bundle `d20e3523f73671364f9187d89e8d609733abfd0f5bf83423d6a99e7bdf167031`.

The run failed closed before publishing an actual artifact because the model proposed `edit_daily_item` without the required `daily_item_target` entity binding. The production contract rejected that malformed proposal with `ValueError`. The runner did not have the sealed labels, scoring was not executed, and the unchanged nondeterministic run was not retried.

Evidence: `evals/agent2/semantic_admission/actual_after_fix_failure.json`.

Result:

- attempt count: `1`;
- actual artifact created: `false`;
- scoring performed: `false`;
- acceptance eligible: `false`;
- Enforce advancement: blocked;
- effect on current Shadow deployment: none; Shadow remains audit-only and Enforce remains off.
