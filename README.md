# Workflow Runner

YAML-configurable LLM+script pipeline with **hard constraints** and **Python-level loop** support.

Unlike prompt-based workflow tools that rely on the LLM "remembering" to follow steps, Workflow Runner enforces the flow externally — Python controls the DAG, not the model. Each LLM step gets a fresh agent session (clean context), and failures trigger real loop-backs.

## Features

- **YAML-driven** — define workflows as config, not code
- **Hard-constraint DAG** — Python enforces execution order, not LLM "自觉"
- **Python-level loops** — `goto` jumps back to any node on success; `on_failure: goto` on failure
- **Human-in-the-loop** — approval nodes with file preview and choice routing, route to any step
- **Fresh context per step** — each LLM step can be `session: shared` (default) or `session: new`
- **State persistence** — resume from any step after interruption
- **Portable skills** — `skill_dir/SKILL.md` + `README.md`, copy-and-use

## Quick Start

```bash
pip install -r requirements.txt
```

```bash
python3 runner.py -w examples/online_dev.yaml -t "开发用户登录功能"
```

## How It Works

```
workflow.yaml          skill/SKILL.md        scripts/*.sh
      │                    │                     │
      └────────────────────┼─────────────────────┘
                           │
                     runner.py (unchanged)
                           │
                     ┌─────┼─────┐
                     ▼     ▼     ▼
                  Claude  Bash  Human
                  Agent   Cmd   Input
```

## Execution Engine

The runner's core is a single `while` loop that repeatedly picks the next ready step
from the DAG until no more steps are eligible or the max-iteration guard trips.

### Main Loop

```
while iteration < max_iterations:
    step = _next_ready_step(executed)   -- pick first ready step (see below)
    if step is None → break (all done)

    result = await _execute_step(step)  -- dispatch by step.type

    if result == "failed":
        if step has on_failure: goto → clear target + downstream, loop again
        else: exit(1)

    executed.add(step.id)

    if step.type == "approval" and result.next:
        handle_approval_routing()       -- set preferred_next, don't clear self

    if step.goto and not result.skip_goto:
        handle_goto()                   -- clear target + downstream, loop again
```

### Step Selection (`_next_ready_step`)

Steps are scanned **in YAML definition order**. For each step, if it's not yet in
`executed` AND all its `depends_on` steps are in `executed`, it's "ready" and
returned immediately.

```
for step in workflow.steps (top-down):
    if step.id in executed → skip
    if all(step.depends_on) in executed → return step
return None
```

**`_preferred_next` priority:** when an approval step routes to a `next` step,
`_preferred_next` is set to that step's ID. On the next iteration,
`_next_ready_step` checks the preferred step FIRST (returning it if ready) before
falling through to normal top-down search. This ensures approval routing takes
precedence over definition order.

### Approval Routing

When a `type: approval` step returns a `next` field (from user's choice):

```
result = {status: "ok", choice: "驳回", next: "write_feedback"}

→ executed.discard(next_step)          # un-mark the target so it can run
→ clear_downstream(next_step)          # un-mark everything that depends on it
→ _preferred_next = next_step          # prioritize this step next iteration
```

**Crucially, the approval step itself is NOT cleared from `executed`.** This way:
- If `next` is a downstream step like `stop_jupyter` (which `depends_on` the
  approval), the dep IS satisfied → stop_jupyter runs.
- If `next` is a loop-back step like `write_feedback` (which also depends on the
  approval), it also runs. Then its `goto` is responsible for unwinding the
  downstream chain (see next section).

Without this design, clearing the approval step would make downstream steps
unsatisfied, while the step *before* the approval (whose deps are still met)
would be picked again → infinite loop.

### Goto (Loop-back)

When a step has `goto: <target>`, after the step succeeds:

```
executed.discard(goto_target)          # un-mark the jump target
clear_downstream(goto_target)          # un-mark everything downstream of target
executed.discard(step.id)              # un-mark the goto step itself
```

The key detail is `_clear_downstream(goto_target)`, NOT
`_clear_downstream(step.id)`. Clearing the **target's** downstream ensures that
when jumping back to `draw_chart`, all later steps (`review`, `write_feedback`,
`stop_jupyter`) are also cleared so the entire chain re-executes.

### `skip_goto` Flag

A script step can return `{"status": "ok", "skip_goto": true}` to suppress its
own `goto`. This is used when a step is reached by normal DAG ordering (not
explicit routing) and should quietly pass through.

Example: `write_feedback` has `depends_on: [review]` and `goto: draw_chart`.
After the user approves and `stop_jupyter` completes, `write_feedback` becomes
ready by DAG order. It checks that `review.choice != "驳回"`, returns `skip_goto`,
and the workflow ends cleanly instead of looping back.

### State & Cleanup

Each step's result is saved to `<workflow>.state.json` immediately after
execution. The `_clear_downstream(id)` function transitively removes all steps
that depend on `id` (directly or indirectly) from `executed`. This is used by
approval routing, goto, and failure handling to rewind the DAG.

When all steps complete, the MCP client is disconnected on the same event loop
before `break`, avoiding "unclosed transport" warnings on Windows.

### Max Iterations Guard

```
max_iterations = len(steps) × 10
```

If the loop exceeds this threshold (e.g. a misconfigured goto creating an
infinite cycle), the runner exits with an error instead of hanging.

## Step Types

| type | description | key fields |
|------|-------------|------------|
| `llm` | Claude agent session | `skill_dir`, `user_prompt`, `agent`, `output.save_to`, `retry` |
| `script` | Python func (`script: mod:fn`) or subprocess (`command: "..."`) | `script` / `command`, `goto`, `on_failure` |
| `approval` | Human confirmation with choice routing | `message`, `show_file`, `choices[].next` |

### Script Step: Return Flags

A `type: script` function can include these keys in its return dict:

| flag | effect |
|------|--------|
| `skip_goto: true` | Suppress the step's `goto`, even if configured. Use when the step was reached by DAG order (not routing) and should pass through. |
| `status: "failed"` | Trigger `on_failure` handling (goto / abort). |

## Workflow YAML Structure

```yaml
name: "在线代码开发"

context:
  work_dir: "./project"

steps:
  - id: requirement_analysis
    type: llm
    skill_dir: "./skills/requirement_analysis"
    user_prompt: "分析需求：{{ input.task }}"
    retry: 2

  - id: human_review
    type: approval
    depends_on: [requirement_analysis]
    message: "确认需求分析结果？"
    choices:
      - label: "通过"
      - label: "驳回"
        next: requirement_analysis   # ← Python-level loop back

  - id: compile
    type: script
    depends_on: [human_review]
    command: "npm run build"
    on_failure:
      action: goto
      target: failure_analysis      # ← hard-constraint jump
```

## Skill Resolution

Each skill is a directory containing `SKILL.md` (the prompt) + `README.md` (docs).
When you specify a **bare name** in the YAML:

```yaml
skill_dir: "requirement_analysis"   # no path separators = bare name
```

The runner searches three locations in order:

| # | Location | Scope |
|---|----------|-------|
| 1 | `~/.claude/skills/<name>/` | Global — shared across all projects |
| 2 | `<yaml_dir>/.claude/skills/<name>/` | Project — Claude Code convention |
| 3 | `<yaml_dir>/skills/<name>/` | Project — flat skills directory |

First match wins. If you use a **path** instead (contains `/`, `\`, or starts with `.`), it resolves directly relative to the YAML file:

```yaml
skill_dir: "./my-custom-skills/foo"   # path = direct resolution
```

## Session Model

**Default: shared.** All LLM steps run in the same Claude agent session.
Requirement analysis context (clarifications, decisions, trade-offs) is preserved
and available to code development. Each step's skill is injected as part of the
user message so the agent always knows what to do.

**Opt-in: isolated.** Add `agent.session: new` to spawn a fresh session for a
specific step. The step's skill becomes the system_prompt, and no prior
conversation history leaks in.

```yaml
# 共享 session — 需求分析的历史自动传给代码开发
- id: requirement_analysis
  type: llm
  skill_dir: "./skills/requirement_analysis"
  # agent.session 默认 "shared"

- id: code_development
  type: llm
  agent:
    permission_mode: "acceptEdits"

# 独立 session — CR 看不到开发历史，防止"自己审自己"
- id: code_review
  type: llm
  agent:
    session: new                       # ← 显式隔离
    model: "claude-sonnet-4-6"
    permission_mode: "plan"
    allowed_tools: [Read, Grep, Glob]
```

| agent field | default | description |
|-------------|---------|-------------|
| `session` | `"shared"` | `shared` (preserve history) or `new` (fresh session) |
| `model` | workflow default | Claude model for this step |
| `permission_mode` | `"default"` | `default` / `acceptEdits` / `plan` / `bypassPermissions` |
| `allowed_tools` | all | Tools this agent can use |
| `max_turns` | unlimited | Max tool-calling round-trips |
| `cwd` | workflow `work_dir` | Working directory |

## CLI

```bash
# Basic
python3 runner.py -w workflow.yaml -t "task description"

# Extra inputs (accessible as {{ input.key }} in templates)
python3 runner.py -w workflow.yaml -t "xxx" -i branch "feature/login"

# Auto-approve all human nodes (CI mode)
python3 runner.py -w workflow.yaml -t "xxx" --yes

# Resume from a step
python3 runner.py -w workflow.yaml --resume compile_check

# Dry run
python3 runner.py -w workflow.yaml -t "xxx" --dry-run
```

## Template Context

Inside `user_prompt`, `message`, and `command` fields, use Jinja2 templates:

```
{{ input.task }}                    # CLI --task value
{{ input.key }}                     # CLI -i key value
{{ config.work_dir }}              # context.work_dir from YAML
{{ steps.requirement_analysis.output }}  # previous step output
{{ steps.compile.stderr }}         # script stderr from previous step
{{ env.HOME }}                     # environment variables
```

## Hard Constraints vs Soft Prompts

```
Soft (OpenSpec style):                Hard (Workflow Runner):
                              
prompt tells LLM:                     Python enforces:
"please run openspec status"          ──► subprocess.run()
"if it fails, please retry"           ──► on_failure: goto → real loop
"ask user before proceeding"          ──► input() blocks execution
```

## Requirements

- Python 3.10+
- `claude-agent-sdk` (Claude Code must be installed and authenticated)
- Git (for script steps that use git)

## License

MIT
