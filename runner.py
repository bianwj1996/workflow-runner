#!/usr/bin/env python3
"""
Workflow Runner — YAML-configurable LLM+script pipeline with hard constraints.

Default: all LLM steps share ONE Claude agent session, preserving conversation
context (decisions, clarifications) across steps like requirement_analysis → code_development.

Opt-in isolation: agent.session: "new" gives a step its own fresh session (e.g. CR).

Usage:
    python3 runner.py --workflow online_dev.yaml --task "开发用户登录功能"
    python3 runner.py -w online_dev.yaml --task "xxx" --resume compile_check
    python3 runner.py -w online_dev.yaml --task "xxx" --yes --dry-run
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        ResultMessage,
        TextBlock,
        ThinkingBlock,
        ToolUseBlock,
    )
except ImportError:
    print("claude-agent-sdk not installed. Run: pip install claude-agent-sdk")
    sys.exit(1)


# ── Path resolution ───────────────────────────────────────────
def resolve_path(path: str, base_dir: Path) -> str:
    """Resolve a path: absolute paths stay, relative paths resolve against base_dir."""
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str((base_dir / p).resolve())


# ── Template rendering ─────────────────────────────────────────
def render(template: str, ctx: dict) -> str:
    return Template(template).render(**ctx)


def render_file(path: str, ctx: dict) -> str:
    return render(Path(path).read_text(encoding="utf-8"), ctx)


# ── Context helpers ────────────────────────────────────────────
def build_context(config: dict, inputs: dict, state: dict, base_dir: Path) -> dict:
    ctx: dict[str, Any] = {
        "input": inputs,
        "env": os.environ.copy(),
        "config": config.get("context", {}),
        "steps": {},
        "workflow_dir": str(base_dir),
    }
    for step_id, result in state.get("steps", {}).items():
        ctx["steps"][step_id] = result
    return ctx


# ── Skill resolution ───────────────────────────────────────────
def resolve_skill_dir(skill_dir: str, base_dir: Path) -> str:
    skill_path = Path(resolve_path(skill_dir, base_dir))
    if not skill_path.is_dir():
        raise FileNotFoundError(f"Skill directory not found: {skill_dir}")
    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        raise FileNotFoundError(f"SKILL.md not found in skill directory: {skill_dir}")
    return str(skill_md)


def read_skill(step: dict, ctx: dict, base_dir: Path) -> str:
    """Resolve and read the skill content for a step."""
    if "skill_dir" in step:
        skill_dir = render(step["skill_dir"], ctx)
        return Path(resolve_skill_dir(skill_dir, base_dir)).read_text(encoding="utf-8")
    elif "system_prompt_file" in step:
        return render_file(resolve_path(render(step["system_prompt_file"], ctx), base_dir), ctx)
    elif "system_prompt" in step:
        return render(step["system_prompt"], ctx)
    raise ValueError("LLM step requires 'skill_dir', 'system_prompt_file', or 'system_prompt'")


# ── Agent options builder ──────────────────────────────────────
def build_agent_options(step: dict, ctx: dict, system_prompt: str, base_dir: Path) -> ClaudeAgentOptions:
    agent = {**step.get("agent", {})}
    cwd = agent.get("cwd") or step.get("cwd") or ctx.get("config", {}).get("work_dir") or os.getcwd()
    cwd = render(cwd, ctx) if cwd else cwd
    cwd = resolve_path(cwd, base_dir) if cwd else cwd
    model = agent.get("model") or step.get("model") or ctx.get("config", {}).get("model")
    permission_mode = agent.get("permission_mode", step.get("permission_mode", "default"))
    max_turns = agent.get("max_turns") or step.get("max_turns")
    allowed_tools = agent.get("allowed_tools") or step.get("allowed_tools") or ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]
    disallowed_tools = agent.get("disallowed_tools") or step.get("disallowed_tools") or []
    thinking = agent.get("thinking") or step.get("thinking")

    opts = ClaudeAgentOptions(
        system_prompt=system_prompt,
        permission_mode=permission_mode,
        cwd=cwd,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )
    if model:
        opts.model = model
    if max_turns is not None:
        opts.max_turns = max_turns
    if thinking is not None:
        opts.thinking = thinking
    return opts


# ── Script step executor ───────────────────────────────────────
def run_script(step: dict, ctx: dict, base_dir: Path) -> dict:
    cmd = render(step["command"], ctx)
    cwd = step.get("cwd")
    if cwd:
        cwd = resolve_path(render(cwd, ctx), base_dir)
    else:
        cwd = str(base_dir)
    timeout = step.get("timeout", 300)

    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            cwd=cwd, timeout=timeout,
        )
        return {
            "status": "ok" if proc.returncode == 0 else "failed",
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"status": "failed", "error": str(e)}


# ── Approval step executor ─────────────────────────────────────
def run_approval(step: dict, ctx: dict, auto_yes: bool, base_dir: Path) -> dict:
    if auto_yes:
        print(f"  [auto-yes] Skipping approval: {step.get('description', step['id'])}")
        return {"status": "ok", "approved": True, "auto": True}

    msg = render(step.get("message", "Proceed?"), ctx)
    show = step.get("show_file")
    if show:
        fp = resolve_path(render(show, ctx), base_dir)
        if Path(fp).exists():
            print(f"\n{'-'*40}\n{Path(fp).read_text(encoding='utf-8')}\n{'-'*40}\n")

    print(f"\n{'='*50}\n{msg}\n{'='*50}")

    choices = step.get("choices")
    if choices:
        labels = [c["label"] for c in choices]
        prompt = " | ".join(f"[{i+1}] {l}" for i, l in enumerate(labels))
        while True:
            resp = input(f"{prompt}\n> ").strip()
            if resp.isdigit() and 1 <= int(resp) <= len(labels):
                chosen = choices[int(resp) - 1]
                return {
                    "status": "ok", "approved": True,
                    "choice": chosen["label"], "next": chosen.get("next"),
                }
            print("Invalid choice.")
    else:
        resp = input("Confirm? [y/n]: ").strip().lower()
        if resp == "y":
            return {"status": "ok", "approved": True}
        return {"status": "rejected", "approved": False}


# ── Main runner ────────────────────────────────────────────────
class WorkflowRunner:
    def __init__(self, config_path: str, auto_yes: bool = False, dry_run: bool = False):
        self.config_path = Path(config_path).resolve()
        self.base_dir = self.config_path.parent  # all relative paths resolve from YAML's directory
        self.config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.auto_yes = auto_yes
        self.dry_run = dry_run
        self.state_path = Path(config_path).with_suffix(".state.json")
        self.state: dict = {"steps": {}, "meta": {}}
        self.ctx: dict = {}

        # Session management
        self._client: ClaudeSDKClient | None = None
        self._session_label: str = ""

    def run(self, inputs: dict, resume_from: str | None = None):
        try:
            asyncio.run(self._run(inputs, resume_from))
        finally:
            if self._client:
                try:
                    asyncio.run(self._client.disconnect())
                except Exception:
                    pass

    # ── Session management ──────────────────────────────────

    async def _ensure_client(self, step: dict) -> ClaudeSDKClient:
        """Get or create the ClaudeSDKClient for this step.

        session: "shared" (default) — reuse existing client, preserve history
        session: "new"            — disconnect old, create fresh isolated client
        """
        session_mode = step.get("agent", {}).get("session", "shared")
        skill = read_skill(step, self.ctx, self.base_dir)

        if session_mode == "new":
            # Tear down shared session if any
            if self._client:
                await self._client.disconnect()
                self._client = None
                self._session_label = ""

            step_label = f"{step['id']} (isolated)"
            print(f"  [NEW] isolated session: {step_label}")
            options = build_agent_options(step, self.ctx, skill, self.base_dir)
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
            self._session_label = step_label
            return self._client

        # session: shared — reuse or create shared session
        if self._client is None:
            print(f"  [SHARED] session: {step['id']} (first LLM step)")
            options = build_agent_options(step, self.ctx, skill, self.base_dir)
            self._client = ClaudeSDKClient(options=options)
            await self._client.connect()
            self._session_label = "shared"
        else:
            print(f"  [SHARED] session: {step['id']} (cont from {self._session_label})")

        return self._client

    # ── LLM step executor ────────────────────────────────────

    async def _run_llm(self, step: dict) -> dict:
        skill = read_skill(step, self.ctx, self.base_dir)
        client = await self._ensure_client(step)
        session_mode = step.get("agent", {}).get("session", "shared")

        if session_mode == "new":
            # Isolated session: skill = system_prompt, user_prompt = task only
            user_message = render(step["user_prompt"], self.ctx)
        else:
            # Shared session: skill goes into user message as step instructions
            user_message = render(
                "## Step Instructions\n\n"
                f"{skill}\n\n"
                "## Task\n\n"
                f"{render(step['user_prompt'], self.ctx)}",
                self.ctx,
            )

        agent_cfg = step.get("agent", {})
        model = agent_cfg.get("model") or step.get("model") or self.config.get("context", {}).get("model")
        retries = step.get("retry", 0) + 1
        all_text: list[str] = []
        usage_info: dict[str, Any] = {}
        cost = 0.0
        duration_ms = 0
        final_subtype = ""
        last_output = ""

        for attempt in range(retries):
            try:
                all_text = []
                await client.query(user_message)

                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                all_text.append(block.text)
                            elif isinstance(block, ToolUseBlock):
                                print(f"  [TOOL] {block.name}({json.dumps(block.input, ensure_ascii=False)[:120]})")
                            elif isinstance(block, ThinkingBlock):
                                print(f"  [THINK] {block.thinking[:200]}...")

                    elif isinstance(message, ResultMessage):
                        cost = message.total_cost_usd or 0
                        duration_ms = message.duration_ms or 0
                        final_subtype = message.subtype or ""
                        if message.usage:
                            usage_info = message.usage
                        if message.is_error:
                            raise RuntimeError(
                                f"Agent error: {message.errors}, subtype={message.subtype}"
                            )

                combined_text = "\n".join(all_text)
                last_output = combined_text

                save_path = step.get("output", {}).get("save_to")
                if save_path:
                    dst = resolve_path(render(save_path, self.ctx | {"_last_output": combined_text}), self.base_dir)
                    Path(dst).parent.mkdir(parents=True, exist_ok=True)
                    Path(dst).write_text(combined_text, encoding="utf-8")

                if final_subtype == "error_during_execution":
                    raise RuntimeError(f"Agent ended with error: {combined_text[-500:]}")

                return {
                    "status": "ok",
                    "output": combined_text,
                    "model": model or "default",
                    "cost_usd": cost,
                    "duration_ms": duration_ms,
                    "usage": usage_info,
                    "subtype": final_subtype,
                }

            except Exception as e:
                if attempt < retries - 1:
                    print(f"  [RETRY] Attempt {attempt + 1} failed: {e}, retrying...")
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {"status": "failed", "error": str(e), "output": last_output}

        return {"status": "failed", "error": "max retries exceeded"}

    # ── Step dispatch ────────────────────────────────────────

    async def _execute_step(self, step: dict) -> dict:
        if step["type"] == "llm":
            return await self._run_llm(step)
        elif step["type"] == "script":
            return run_script(step, self.ctx, self.base_dir)
        elif step["type"] == "approval":
            return run_approval(step, self.ctx, self.auto_yes, self.base_dir)
        else:
            return {"status": "failed", "error": f"Unknown step type: {step['type']}"}

    # ── Main loop ────────────────────────────────────────────

    async def _run(self, inputs: dict, resume_from: str | None = None):
        self.ctx = build_context(self.config, inputs, self.state, self.base_dir)

        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.ctx = build_context(self.config, inputs, self.state, self.base_dir)

        self.state["meta"]["config"] = str(self.config_path)
        self.state["meta"]["started"] = datetime.now().isoformat()
        self._save_state()

        executed: set[str] = set()
        if resume_from:
            step_ids = [s["id"] for s in self.config["steps"]]
            try:
                resume_idx = step_ids.index(resume_from)
                executed = set(step_ids[:resume_idx])
                for sid in step_ids[resume_idx:]:
                    self.state["steps"].pop(sid, None)
            except ValueError:
                print(f"Error: step '{resume_from}' not found in workflow")
                sys.exit(1)
        else:
            executed = {
                sid for sid, s in self.state["steps"].items() if s.get("status") == "ok"
            }

        self._save_state()
        max_iterations = len(self.config["steps"]) * 10
        iteration = 0

        while iteration < max_iterations:
            iteration += 1
            step = self._next_ready_step(executed)
            if step is None:
                print("\n[OK] All steps completed.")
                break

            print(f"\n{'='*60}")
            print(f">> [{step['id']}] {step.get('description', '')}")
            session = step.get("agent", {}).get("session", "shared") if step["type"] == "llm" else "-"
            print(f"  type: {step['type']}  session: {session}")

            if self.dry_run:
                print(f"  [dry-run] Would execute, skipping.")
                executed.add(step["id"])
                continue

            result = await self._execute_step(step)
            self.state["steps"][step["id"]] = {
                **result, "executed_at": datetime.now().isoformat()
            }
            self._save_state()
            self.ctx = build_context(self.config, inputs, self.state, self.base_dir)

            if result["status"] == "failed":
                print(f"[FAIL] [{step['id']}] FAILED: {result.get('error') or result.get('stderr', '')}")
                target = self._handle_failure(step)
                if target:
                    print(f"<- jump to: {target}")
                    executed.discard(target)
                    executed = self._clear_downstream(step["id"], executed)
                    continue
                else:
                    print("[FAIL] No fallback target, aborting.")
                    self._save_state()
                    sys.exit(1)

            if result["status"] == "rejected":
                print(f"[FAIL] [{step['id']}] Rejected, aborting.")
                self._save_state()
                sys.exit(1)

            print(f"[OK] [{step['id']}] OK")
            executed.add(step["id"])

            if step["type"] == "approval" and result.get("next"):
                next_step = result["next"]
                if next_step != "archive":
                    print(f"-> route to: {next_step}")
                    executed.discard(next_step)
                    executed = self._clear_downstream(next_step, executed)

        if iteration >= max_iterations:
            print("[FAIL] Max iterations reached, possible infinite loop.")
            sys.exit(1)

    # ── DAG helpers ──────────────────────────────────────────

    def _next_ready_step(self, executed: set[str]) -> dict | None:
        for step in self.config["steps"]:
            if step["id"] in executed:
                continue
            deps = step.get("depends_on", [])
            if all(d in executed for d in deps):
                return step
        return None

    def _handle_failure(self, step: dict) -> str | None:
        on_fail = step.get("on_failure")
        if on_fail and on_fail.get("action") == "goto":
            return on_fail["target"]
        return None

    def _clear_downstream(self, step_id: str, executed: set[str]) -> set[str]:
        to_clear = {step_id}
        changed = True
        while changed:
            changed = False
            for step in self.config["steps"]:
                if step["id"] in to_clear:
                    continue
                deps = set(step.get("depends_on", []))
                if to_clear & deps and step["id"] not in to_clear:
                    to_clear.add(step["id"])
                    changed = True
        return executed - to_clear

    def _save_state(self):
        self.state["meta"]["updated"] = datetime.now().isoformat()
        self.state_path.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8"
        )


# ── CLI ────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Workflow Runner — YAML-configurable LLM+script pipeline"
    )
    parser.add_argument("-w", "--workflow", required=True, help="Workflow YAML file")
    parser.add_argument("-t", "--task", help="Task description")
    parser.add_argument(
        "-i", "--input", action="append", nargs=2, metavar=("KEY", "VALUE"),
        default=[], help="Extra input key=value pairs",
    )
    parser.add_argument("--resume", help="Resume from a specific step ID")
    parser.add_argument("--state", help="State file path (default: derived from workflow)")
    parser.add_argument("--yes", action="store_true", help="Auto-approve all approval nodes")
    parser.add_argument("--dry-run", action="store_true", help="Show steps without executing")
    parser.add_argument("--no-state", action="store_true", help="Don't save state on success")

    args = parser.parse_args()

    inputs = {"task": args.task or ""}
    for k, v in args.input:
        inputs[k] = v

    runner = WorkflowRunner(args.workflow, auto_yes=args.yes, dry_run=args.dry_run)

    if args.state:
        runner.state_path = Path(args.state)

    runner.run(inputs, resume_from=args.resume)

    if not args.dry_run and not args.no_state:
        print(f"\nState file: {runner.state_path}")
    elif args.no_state and runner.state_path.exists():
        runner.state_path.unlink()


if __name__ == "__main__":
    main()
