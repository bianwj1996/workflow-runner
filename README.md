# Workflow Runner

YAML-configurable LLM+script pipeline with **hard constraints** and **Python-level loop** support.

Unlike prompt-based workflow tools that rely on the LLM "remembering" to follow steps, Workflow Runner enforces the flow externally — Python controls the DAG, not the model. Each LLM step gets a fresh agent session (clean context), and failures trigger real loop-backs.

## Features

- **YAML-driven** — define workflows as config, not code
- **Hard-constraint DAG** — Python enforces execution order, not LLM "自觉"
- **Python-level loops** — `on_failure: goto` jumps back to any upstream node
- **Fresh context per step** — each LLM step is a new `claude-agent-sdk` session
- **Human-in-the-loop** — approval nodes with file preview and choice routing
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

## Step Types

| type | description | key fields |
|------|-------------|------------|
| `llm` | Fresh Claude agent session | `skill_dir`, `user_prompt`, `agent`, `output.save_to` |
| `script` | Shell command | `command`, `on_failure` |
| `approval` | Human confirmation | `message`, `show_file`, `choices` |

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

## Skill Directory

```
skills/requirement_analysis/
├── SKILL.md      # The system prompt injected into Claude
└── README.md     # Description, inputs, outputs, dependencies
```

Reference in YAML:

```yaml
skill_dir: "./skills/requirement_analysis"
```

To use a skill from anywhere, just copy the directory — no code changes needed.

## Agent Isolation

Each `llm` step spawns a **brand new Claude agent session** — no context leaks between steps. The `agent` block makes this explicit and lets you configure different models or permissions per step:

```yaml
# 代码开发 agent — 需要写权限
- id: code_development
  type: llm
  agent:
    permission_mode: "acceptEdits"
    allowed_tools: [Read, Write, Edit, Bash, Glob, Grep]

# CR agent — 只读，不同模型，防止"自己审自己"
- id: code_review
  type: llm
  agent:
    model: "claude-opus-4-7"
    permission_mode: "plan"
    allowed_tools: [Read, Grep, Glob]    # 只能读
    max_turns: 15
```

| agent field | default | description |
|-------------|---------|-------------|
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
