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

## Installation

```bash
# Dev mode (recommended) — edit runner.py without reinstalling
pip install -e .

# Prod mode — install into site-packages
pip install .
```

Once installed, use the `wfr` or `workflow-runner` command:

```bash
# Quick test
cd examples
wfr -w online_dev.yaml -t "用python写一个hello world"
```

Or just install dependencies and run directly with python:

```bash
pip install -r requirements.txt
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

## YAML Reference

### Top-level fields

| field | required | type | description |
|-------|----------|------|-------------|
| `name` | no | string | Human-readable workflow name |
| `context` | no | dict | Default settings for all steps (see below) |
| `steps` | **yes** | list | Ordered list of step definitions |

### Context fields

Nested under `context:`.

| field | required | type | default | description |
|-------|----------|------|---------|-------------|
| `work_dir` | no | string | `os.getcwd()` | Default working directory for all steps |
| `model` | no | string | — | Default Claude model for all LLM steps |
| `max_iterations` | no | int | `steps × 10` | Max loop iterations before aborting (safety guard) |

All context keys are available in templates as `{{ config.<key> }}`.

### Step fields (all types)

| field | required | type | default | description |
|-------|----------|------|---------|-------------|
| `id` | **yes** | string | — | Unique step identifier |
| `type` | **yes** | string | — | `llm` / `script` / `approval` |
| `description` | no | string | `id` | Human-readable label for logging |
| `depends_on` | no | list[string] | `[]` | Step IDs that must complete before this one |
| `goto` | no | string | — | Step ID to jump to after success (unless `skip_goto`) |
| `cwd` | no | string | `context.work_dir` | Working directory for this step |
| `timeout` | no | int | `300` | Timeout in seconds (script steps only) |
| `on_failure` | no | dict | — | Failure handler (see below) |

Agent-related step fields (also settable under `agent:` which overrides these):

| field | type | default | description |
|-------|------|---------|-------------|
| `model` | string | `context.model` | Claude model |
| `permission_mode` | string | `"default"` | `default` / `acceptEdits` / `plan` / `bypassPermissions` |
| `max_turns` | int | — | Max tool-calling round-trips |
| `allowed_tools` | list[string] | `[Read, Write, Edit, Bash, Glob, Grep]` | Allowed tool names |
| `disallowed_tools` | list[string] | `[]` | Disallowed tool names |
| `thinking` | bool | — | Extended thinking toggle |
| `mcp_servers` | dict | — | MCP server config (see below) |

### LLM step (`type: llm`)

| field | required | type | description |
|-------|----------|------|-------------|
| `skill_dir` | * | string | Skill name or path. Bare name searches `~/.claude/skills/`, `<project>/.claude/skills/`, `<project>/skills/` in order |
| `system_prompt` | * | string | Inline system prompt text |
| `system_prompt_file` | * | string | Path to system prompt file (relative to YAML) |
| `user_prompt` | **yes** | string | Main user message (Jinja2 template) |
| `retry` | no | int | Retry count on failure (default: `0`) |
| `output` | no | dict | Output config (see below) |

\* Exactly one of `skill_dir`, `system_prompt`, or `system_prompt_file` is required.

### Script step (`type: script`)

| field | required | type | description |
|-------|----------|------|-------------|
| `script` | * | string | `"module:function"` — direct Python call |
| `command` | * | string | Shell command string |
| `timeout` | no | int | Command timeout in seconds (default: `300`) |

\* `script` takes priority. One of the two is required.

A script function can return flags: `skip_goto: true` suppresses the step's `goto`; `status: "failed"` triggers `on_failure` handling.

### Approval step (`type: approval`)

| field | required | type | default | description |
|-------|----------|------|---------|-------------|
| `message` | no | string | `"Proceed?"` | Prompt shown to user (Jinja2 template) |
| `show_file` | no | string | — | File path to display before the prompt |
| `choices` | no | list[dict] | y/n | Named choices (see below) |

### Agent fields

Nested under `agent:`. All fields here override the step-level equivalents.

| field | type | default | description |
|-------|------|---------|-------------|
| `session` | string | `"shared"` | `"shared"` (reuse agent session) or `"new"` (isolated session) |
| `model` | string | step `model` | Claude model |
| `permission_mode` | string | step `permission_mode` | Permission mode |
| `max_turns` | int | step `max_turns` | Max tool-calling rounds |
| `allowed_tools` | list[string] | step `allowed_tools` | Allowed tools |
| `disallowed_tools` | list[string] | step `disallowed_tools` | Disallowed tools |
| `thinking` | bool | step `thinking` | Extended thinking |
| `cwd` | string | step `cwd` | Working directory |
| `mcp_servers` | dict | step `mcp_servers` | MCP server config |

### MCP servers

Nested under `mcp_servers:` (or `agent.mcp_servers:`). Each key is a server name.

| field | required | type | description |
|-------|----------|------|-------------|
| `command` | **yes** | string | MCP server executable |
| `args` | no | list[string] | Command-line arguments |
| `env` | no | dict | Environment variables (values are Jinja2-templated) |

### Output

Nested under `output:` on LLM steps.

| field | required | type | description |
|-------|----------|------|-------------|
| `save_to` | **yes** | string | File path to write LLM output (Jinja2 template, `_last_output` available) |

### Choices

Nested under `choices:` on approval steps. Each item is a dict.

| field | required | type | description |
|-------|----------|------|-------------|
| `label` | **yes** | string | Display text for the choice |
| `next` | no | string | Step ID to route to when chosen |

### On failure

Nested under `on_failure:` on any step.

| field | required | type | description |
|-------|----------|------|-------------|
| `action` | **yes** | string | Currently only `"goto"` is supported |
| `target` | **yes** | string | Step ID to jump to on failure |

### Example

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

### Template variables

Available in all Jinja2-templated fields (`user_prompt`, `message`, `command`, `env` values, etc.):

| variable | type | description |
|----------|------|-------------|
| `{{ input }}` | dict | CLI inputs: `input.task` from `--task`, plus `-i key value` pairs |
| `{{ env }}` | dict | All environment variables |
| `{{ config }}` | dict | The `context:` section contents |
| `{{ steps }}` | dict | Completed step results by ID: `steps.<id>.output`, `.status`, `.stderr`, etc. |
| `{{ workflow_dir }}` | string | Absolute path of the YAML file's directory |

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
