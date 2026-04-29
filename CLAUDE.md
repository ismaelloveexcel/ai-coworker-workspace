# AI Coworker Agent Contract

## MANDATORY OUTPUT FORMAT (strict — no deviation)

```
PLAN:
- step 1
- step 2
- step 3

ACTION: tool_call | final_answer | error
TOOL: {tool_name}
INPUT: {json}
REASONING: {short explanation}
```

## RULES
- NEVER skip PLAN
- NEVER skip ACTION
- NO free text outside the format above
- Always include REASONING
- Must be deterministic and parseable

## GIT RULES
- Always operate on branches: `task/{task_id}`
- NEVER touch `main` directly
- One task = one branch = one PR
- No secrets in output or commits

## TOOL NAMES
- `github_create_branch`
- `github_commit_files`
- `github_create_pr`
- `github_read_file`
- `github_list_files`
- `filesystem_read`
- `filesystem_write`
- `final_answer`

## TERMINATION
- Emit `ACTION: final_answer` once PR is created
- Include PR URL in INPUT
