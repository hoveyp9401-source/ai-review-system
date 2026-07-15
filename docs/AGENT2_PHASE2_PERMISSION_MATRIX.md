# Agent2 Phase 2 permission matrix

| Operation | Tenant | Company/department/team | User/role | Case scope | Failure behavior |
|---|---|---|---|---|---|
| Resolve party | required | inherited through visible cases | channel-bound actor | allowed case IDs only | not found/clarify |
| Query party cases | required | inherited through visible cases | channel-bound actor | allowed case IDs only | empty result |
| Query party relations/clues | required | related party must occur in visible scope | channel-bound actor | exact party plus allowed case IDs | omit invisible relation/clue |
| Create case progress | required | case record boundary | reporter identity | target must be allowed | typed block |
| Update/delete/link progress | required | case record boundary | owner or `case_progress_admin` | target must be allowed | typed block/version conflict |
| Create travel intent | required | stored from binding | channel-bound actor | linked cases must be allowed | typed block |
| Match travel | required | same company, department and team | different users | no case details used | no candidate |
| Dispatch notification | required | candidate organization boundary | active DingTalk binding | not disclosed | retry/dead-letter |
| Read Phase 2 evidence | principal tenant | server-side token binding | sandbox principal | tenant-filtered | 404 outside allowlist |
| Change tenant route | required | tenant admin boundary | audited actor | n/a | version conflict |

Identity is derived from the actual DingTalk channel binding. Names or tenant claims written in message text are never authorization inputs.
