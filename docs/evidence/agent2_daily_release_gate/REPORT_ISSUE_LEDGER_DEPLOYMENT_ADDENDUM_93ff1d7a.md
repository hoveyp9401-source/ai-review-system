# Daily Report Issue Ledger — Deployment Addendum

This addendum records findings from the authorized two-user Canary deployment
of `93ff1d7a`. It does not modify the user-owned root issue ledger.

| ID | Severity | Finding | Disposition | Evidence |
|---|---|---|---|---|
| DAILY-DEPLOY-20260722-01 | Closed operational | Non-interactive `systemctl` control was unavailable. The attempt stopped before file replacement. | Switched to an owner-process, systemd-supervised restart procedure. | `deployment_failed.json`; final services active. |
| DAILY-DEPLOY-20260722-02 | Closed operational | First owner-signal deployment encountered root-owned Python cache content. | Automatic rollback restored all 12 files byte-for-byte before retry. Cache content was excluded from the explicit file deployment. | `deployment_signal_round1_pycache_failure.json`; `after_pycache_rollback.sha256`. |
| DAILY-DEPLOY-20260722-03 | P2 legacy gate | Three previous-report cutoff tests fail in current production. | Open; outside the 12-file candidate scope and not claimed fixed. | `product_gate_quick.txt`: 137 passed, 3 failed. |
| DAILY-DEPLOY-20260722-04 | P1 watch | DR-012 range merge remains unstable/incorrect. | Open. Candidate 0/3 and backed-up baseline 0/3, so it is not candidate-only. | Candidate/baseline DR-012 artifacts. |
| DAILY-DEPLOY-20260722-05 | Closed observation | DR-017 failed once in the 104-case run. | Candidate repeat 3/3 and baseline 1/1; classified as transient model variance, retained in evidence. | Full smoke and DR-017 repeat artifacts. |
| DAILY-DEPLOY-20260722-06 | Closed cleanup | One exact synthetic issue-smoke team remained after interrupted smoke cleanup. | Deleted only after confirming one exact row and zero referencing users; all final synthetic counters are zero. | `final_deployment_db_verify.json`. |
| DAILY-DEPLOY-20260722-07 | Product scope | Universal Case/Travel/future-domain projection into Daily is not part of this candidate. | Open and explicitly excluded from rollout claims. | Deployment report scope statement. |
