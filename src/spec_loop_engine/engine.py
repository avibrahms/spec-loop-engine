from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .schemas import RUNNER_RESULT_SCHEMA, VERIFIER_RESULT_SCHEMA
from .spec_loader import PhaseConfig, SpecConfig, StepConfig
from .utils import append_jsonl, now_utc, render_value, write_json, write_text


class SpecLoopError(RuntimeError):
    pass


def _default_state(spec: SpecConfig, run_id: str) -> dict[str, Any]:
    phases: dict[str, Any] = {}
    for phase in spec.phases:
        phases[phase.id] = {
            "title": phase.title,
            "status": "pending",
            "attempts": [],
        }

    return {
        "run_id": run_id,
        "spec_name": spec.name,
        "spec_path": str(spec.path),
        "workspace": str(spec.workspace),
        "status": "running",
        "started_at": now_utc(),
        "updated_at": now_utc(),
        "phases": phases,
    }


def _load_state(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(state_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_utc()
    write_json(state_path, state)


def _journal(journal_path: Path, event: str, **payload: Any) -> None:
    append_jsonl(journal_path, {"time": now_utc(), "event": event, **payload})


def _spec_snapshot(spec: SpecConfig) -> dict[str, Any]:
    return {
        "name": spec.name,
        "path": str(spec.path),
        "workspace": str(spec.workspace),
        "run_root": str(spec.run_root),
        "vars": spec.vars,
        "phases": [
            {
                "id": phase.id,
                "title": phase.title,
                "max_attempts": phase.max_attempts,
                "vars": phase.vars,
                "run": phase.run.config,
                "verify": phase.verify.config,
            }
            for phase in spec.phases
        ],
    }


def _ensure_run_root_ignored(spec: SpecConfig) -> None:
    workspace = spec.workspace.resolve()
    run_root = spec.run_root.resolve()
    try:
        relative = run_root.relative_to(workspace)
    except ValueError:
        return

    completed = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=str(workspace),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return

    git_dir = Path(completed.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (workspace / git_dir).resolve()

    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    pattern = relative.as_posix().rstrip("/") + "/"
    existing = exclude_path.read_text(encoding="utf-8").splitlines() if exclude_path.exists() else []
    if pattern in existing:
        return

    prefix = "\n" if exclude_path.exists() and exclude_path.read_text(encoding="utf-8") and not exclude_path.read_text(encoding="utf-8").endswith("\n") else ""
    with exclude_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{prefix}{pattern}\n")


def _git_command(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(workspace),
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        return None
    return completed


def _git_snapshot(workspace: Path) -> dict[str, Any] | None:
    inside = _git_command(workspace, ["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside.stdout.strip() != "true":
        return None

    branch = _git_command(workspace, ["branch", "--show-current"])
    head = _git_command(workspace, ["rev-parse", "HEAD"])
    branches = _git_command(workspace, ["for-each-ref", "--format=%(refname:short)", "refs/heads"])
    return {
        "branch": branch.stdout.strip() if branch is not None else "",
        "head": head.stdout.strip() if head is not None else "",
        "branches": sorted(branches.stdout.splitlines()) if branches is not None else [],
    }


def _git_followup_note(workspace: Path, start_snapshot: dict[str, Any] | None) -> str | None:
    if start_snapshot is None:
        return None

    issues: list[str] = []
    end_snapshot = _git_snapshot(workspace)
    status = _git_command(workspace, ["status", "--porcelain", "--untracked-files=all"])

    if end_snapshot is None or status is None:
        issues.append(
            "Git state could not be verified. Restore the repo to a normal git worktree, keep all work on the current branch, and return success only when verification can read git state cleanly."
        )
    else:
        if start_snapshot.get("branch") != end_snapshot.get("branch"):
            issues.append(
                f"The active branch changed from `{start_snapshot.get('branch') or '(detached)'}` to `{end_snapshot.get('branch') or '(detached)'}`. Stay on the original branch for this phase."
            )

        start_branches = set(start_snapshot.get("branches", []))
        end_branches = set(end_snapshot.get("branches", []))
        added = sorted(end_branches - start_branches)
        removed = sorted(start_branches - end_branches)
        if added or removed:
            details: list[str] = []
            if added:
                details.append(f"added branch refs: {', '.join(added)}")
            if removed:
                details.append(f"removed branch refs: {', '.join(removed)}")
            issues.append(
                "Branch refs changed during the phase (" + "; ".join(details) + "). This spec requires current-branch checkpoint mode with no branch creation, deletion, renaming, or switching."
            )

        residue = [line for line in status.stdout.splitlines() if line.strip()]
        if residue:
            preview = "; ".join(residue[:8])
            if len(residue) > 8:
                preview += "; ..."
            issues.append(
                f"The worktree is still dirty after the phase implementation ({preview}). Commit or clean every phase artifact before returning success."
            )

    if not issues:
        return None

    bullet_list = "\n".join(f"- {issue}" for issue in issues)
    return (
        "Automatic git finalization follow-up:\n"
        f"{bullet_list}\n"
        "- Do not create, switch, rename, or delete branches.\n"
        "- Interpret any stale plan text about phase branches or exact one-commit branch rules as overridden by current-branch checkpoint mode.\n"
        "- Return `success` only when the current branch is clean and the phase checkpoint commit history is in place."
    )


def _find_latest_unfinished_run(spec: SpecConfig) -> Path | None:
    if not spec.run_root.exists():
        return None
    phase_limits = {phase.id: phase.max_attempts for phase in spec.phases}
    recoverable_phase_statuses = {"running", "verifying", "needs_retry", "needs_verification"}
    candidates = sorted([path for path in spec.run_root.iterdir() if path.is_dir()], reverse=True)
    for candidate in candidates:
        state_path = candidate / "state.json"
        if not state_path.exists():
            continue
        state = _load_state(state_path)
        if state.get("status") == "failed" and any(
            isinstance(phase_state, dict) and phase_state.get("status") in recoverable_phase_statuses
            for phase_state in state.get("phases", {}).values()
        ):
            return candidate
        if state.get("status") == "blocked" and any(
            _needs_verifier_only_retry(phase_state)
            for phase_state in state.get("phases", {}).values()
            if isinstance(phase_state, dict)
        ):
            return candidate
        if state.get("status") == "failed" and any(
            _needs_verifier_only_retry(phase_state)
            for phase_state in state.get("phases", {}).values()
            if isinstance(phase_state, dict)
        ):
            return candidate
        if state.get("status") == "failed":
            for phase_id, phase_state in state.get("phases", {}).items():
                if not isinstance(phase_state, dict) or phase_state.get("status") != "failed":
                    continue
                if len(phase_state.get("attempts", [])) < phase_limits.get(phase_id, 0):
                    return candidate
        if state.get("status") not in {"completed", "failed", "blocked"}:
            return candidate
    return None


def _resolve_run_dir(spec: SpecConfig, fresh: bool) -> Path:
    spec.run_root.mkdir(parents=True, exist_ok=True)
    if not fresh:
        latest = _find_latest_unfinished_run(spec)
        if latest is not None:
            return latest
    run_id = now_utc().replace(":", "").replace("-", "")
    return spec.run_root / run_id


def _write_feedback(phase_dir: Path, verifier_payload: dict[str, Any]) -> Path:
    feedback_path = phase_dir / "latest-verifier-feedback.md"
    lines = [f"# Verifier feedback", "", f"Status: {verifier_payload.get('status', '')}", ""]
    if verifier_payload.get("summary"):
        lines.extend(["## Summary", verifier_payload["summary"], ""])
    issues = verifier_payload.get("issues") or []
    if issues:
        lines.append("## Issues")
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")
    if verifier_payload.get("repair_hint"):
        lines.extend(["## Repair Hint", verifier_payload["repair_hint"], ""])
    write_text(feedback_path, "\n".join(lines).rstrip() + "\n")
    return feedback_path


def _build_context(spec: SpecConfig, run_dir: Path, phase: PhaseConfig, attempt: int, phase_dir: Path, attempt_dir: Path) -> dict[str, Any]:
    latest_feedback_path = phase_dir / "latest-verifier-feedback.md"
    latest_runner_result_path = phase_dir / "latest-runner-result.json"
    latest_verifier_result_path = phase_dir / "latest-verifier-result.json"
    ctx: dict[str, Any] = {
        "spec_name": spec.name,
        "spec_path": str(spec.path),
        "spec_dir": str(spec.spec_dir),
        "workspace": str(spec.workspace),
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "phase_id": phase.id,
        "phase_title": phase.title,
        "phase_dir": str(phase_dir),
        "attempt": attempt,
        "attempt_dir": str(attempt_dir),
        "latest_verifier_feedback_path": str(latest_feedback_path) if latest_feedback_path.exists() else "",
        "latest_runner_result_path": str(latest_runner_result_path) if latest_runner_result_path.exists() else "",
        "latest_verifier_result_path": str(latest_verifier_result_path) if latest_verifier_result_path.exists() else "",
    }
    ctx.update(spec.vars)
    ctx.update(phase.vars)
    return ctx


def _runner_prompt(base_prompt: str, context: dict[str, Any]) -> str:
    feedback_note = (
        f"Prior verifier feedback file: {context['latest_verifier_feedback_path']}"
        if context.get("latest_verifier_feedback_path")
        else "Prior verifier feedback file: none"
    )
    followup_note = context.get("runner_followup_note", "")
    return f"""You are the execution agent for a spec-driven phase runner.

Spec: {context['spec_name']}
Workspace: {context['workspace']}
Phase: {context['phase_id']} — {context['phase_title']}
Attempt: {context['attempt']}
Run directory: {context['run_dir']}
Phase directory: {context['phase_dir']}
{feedback_note}

Rules:
- Do the actual work in the workspace.
- If this is a retry, read the prior verifier feedback and fix every issue before moving on.
- This spec uses current-branch checkpoint mode: do not create, switch, rename, or delete branches, and treat any stale phase-plan language about phase branches, merges, or exact one-commit branch rules as overridden by that policy.
- Do not return status="success" until the current branch is clean, every required checkpoint commit for this phase is already recorded on that branch, and no git residue remains for the verifier to clean up.
- Return ONLY JSON matching the provided schema.
- Use status="success" only when the phase is ready for independent verification.
- Use status="retry" only when more work is still required before verification.
- Use status="blocked" only for external blockers you cannot resolve inside the workspace.
- Always include `notes` even if it is an empty string.

Automatic follow-up note:
{followup_note or "none"}

Phase instructions:
{base_prompt}
"""


def _verifier_prompt(base_prompt: str, context: dict[str, Any]) -> str:
    return f"""You are the independent verifier for a spec-driven phase runner.

Spec: {context['spec_name']}
Workspace: {context['workspace']}
Phase: {context['phase_id']} — {context['phase_title']}
Attempt: {context['attempt']}
Run directory: {context['run_dir']}
Phase directory: {context['phase_dir']}

Rules:
- You are auditing, not implementing.
- Treat this as an independent hidden-corruption and interruption check.
- Do not modify files.
- This spec uses current-branch checkpoint mode: do not require phase branches, merges, or exact one-commit branch counts. The git bar is: no branch operations, required checkpoint commit(s) exist on the current branch, and the worktree is clean.
- Return ONLY JSON matching the provided schema.
- Use status="passed" only when the phase is actually complete and clean.
- Use status="retry" if there is anything left to fix, continue, or verify more concretely.
- Use status="blocked" only for external blockers.
- Always include `confidence` as a number between 0 and 1.

Verification instructions:
{base_prompt}
"""


def _build_prompt(step_kind: str, base_prompt: str, context: dict[str, Any]) -> str:
    if step_kind == "run":
        return _runner_prompt(base_prompt, context)
    return _verifier_prompt(base_prompt, context)


def _read_prompt(step: StepConfig, step_cwd: Path) -> str:
    if "prompt" in step.config:
        return str(step.config["prompt"])
    prompt_file = step.config.get("prompt_file")
    if prompt_file:
        prompt_path = Path(prompt_file)
        if not prompt_path.is_absolute():
            prompt_path = (step_cwd / prompt_path).resolve()
        return prompt_path.read_text(encoding="utf-8")
    raise SpecLoopError(f"Step of type {step.type} is missing prompt or prompt_file")


def _shell_command(step: StepConfig, runtime: dict[str, Any]) -> tuple[list[str] | str, bool]:
    if "command" in step.config:
        return str(step.config["command"]), True
    program = step.config.get("program")
    if not program:
        raise SpecLoopError("shell step requires either command or program")
    args = [str(program)]
    args.extend(str(item) for item in step.config.get("args", []))
    return args, False


def _step_env(step: StepConfig, runtime: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    rendered = render_value({str(k): str(v) for k, v in step.config.get("env", {}).items()}, runtime)
    env.update({str(k): str(v) for k, v in rendered.items()})
    for key in ("HOME", "TMPDIR", "TMP", "TEMP", "XDG_CACHE_HOME"):
        value = env.get(key)
        if value and Path(value).is_absolute():
            Path(value).mkdir(parents=True, exist_ok=True)
    return env


def _step_output_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("stdout_path", "stderr_path"):
        raw_path = payload.get(key)
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if path.exists():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


def _is_codex_auth_failure(payload: dict[str, Any]) -> bool:
    text = _step_output_text(payload).lower()
    return (
        "401 unauthorized" in text
        and (
            "missing bearer" in text
            or "authentication" in text
            or "unauthorized" in text
        )
    )


def _needs_verifier_only_retry(phase_state: dict[str, Any]) -> bool:
    attempts = phase_state.get("attempts", [])
    if not attempts:
        return False
    latest = attempts[-1]
    runner = latest.get("runner", {})
    verifier = latest.get("verifier", {})
    return runner.get("status") == "success" and verifier.get("status") in {"failed", "blocked"}


def _run_subprocess(
    *,
    command: list[str] | str,
    shell: bool,
    cwd: Path,
    env: dict[str, str],
    stdin_text: str | None,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int | None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        shell=shell,
        cwd=str(cwd),
        env=env,
        input=stdin_text,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    write_text(stdout_path, completed.stdout)
    write_text(stderr_path, completed.stderr)
    return completed


def _parse_wait_seconds(text: str) -> int | None:
    lower = text.lower()

    compound = re.search(
        r"(?:try again in|retry in|wait(?:ing)?(?: for)?|resets? in)[^0-9]*(?:(\d+)\s*h(?:ours?)?)?[^0-9]*(?:(\d+)\s*m(?:in(?:ute)?s?)?)?[^0-9]*(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?",
        lower,
    )
    if compound and any(group is not None for group in compound.groups()):
        hours = int(compound.group(1) or 0)
        minutes = int(compound.group(2) or 0)
        seconds = int(compound.group(3) or 0)
        total = hours * 3600 + minutes * 60 + seconds
        if total > 0:
            return total

    simple_seconds = re.search(r"(?:try again in|retry in|wait(?:ing)?(?: for)?|resets? in)[^0-9]*(\d+)\s*(?:sec(?:ond)?s?|s)\b", lower)
    if simple_seconds:
        return int(simple_seconds.group(1))

    simple_minutes = re.search(r"(?:try again in|retry in|wait(?:ing)?(?: for)?|resets? in)[^0-9]*(\d+)\s*(?:min(?:ute)?s?|m)\b", lower)
    if simple_minutes:
        return int(simple_minutes.group(1)) * 60

    simple_hours = re.search(r"(?:try again in|retry in|wait(?:ing)?(?: for)?|resets? in)[^0-9]*(\d+)\s*(?:hour|hours|hr|hrs|h)\b", lower)
    if simple_hours:
        return int(simple_hours.group(1)) * 3600

    return None


def _rate_limit_wait_seconds(
    *,
    stdout: str,
    stderr: str,
    step: StepConfig,
) -> int | None:
    combined = "\n".join(part for part in [stderr, stdout] if part).lower()
    triggers = [
        "rate limit",
        "too many requests",
        "status: 429",
        "http 429",
        "error 429",
        "usage limit",
        "request limit",
        "quota exceeded",
    ]
    if not any(trigger in combined for trigger in triggers):
        return None

    parsed = _parse_wait_seconds(f"{stderr}\n{stdout}")
    if parsed is not None:
        return parsed

    return int(step.config.get("rate_limit_default_wait_seconds", 15 * 60))


def _status_from_exit_codes(step_kind: str, step: StepConfig, returncode: int) -> str:
    passed_key = "passed_exit_codes" if step_kind == "verify" else "success_exit_codes"
    success_codes = set(step.config.get(passed_key, [0]))
    retry_codes = set(step.config.get("retry_exit_codes", []))
    blocked_codes = set(step.config.get("blocked_exit_codes", []))

    if returncode in success_codes:
        return "passed" if step_kind == "verify" else "success"
    if returncode in retry_codes:
        return "retry"
    if returncode in blocked_codes:
        return "blocked"
    return "failed"


def _run_shell_step(
    *,
    step_kind: str,
    step: StepConfig,
    runtime: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
    result_path: Path,
) -> dict[str, Any]:
    cwd = Path(step.config.get("cwd", runtime["workspace"])).resolve()
    env = _step_env(step, runtime)
    timeout_seconds = step.config.get("timeout_seconds")
    command, use_shell = _shell_command(step, runtime)
    completed = _run_subprocess(
        command=command,
        shell=use_shell,
        cwd=cwd,
        env=env,
        stdin_text=None,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=timeout_seconds,
    )

    status = _status_from_exit_codes(step_kind, step, completed.returncode)
    summary = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else f"returncode={completed.returncode}"
    if step_kind == "verify":
        payload = {
            "status": status,
            "summary": summary,
            "issues": [],
            "repair_hint": "",
            "confidence": 1.0 if status == "passed" else 0.0,
            "returncode": completed.returncode,
            "command": command,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    else:
        payload = {
            "status": status,
            "summary": summary,
            "artifacts": [],
            "resume_hint": "",
            "notes": "",
            "returncode": completed.returncode,
            "command": command,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
    write_json(result_path, payload)
    return payload


def _parse_structured_output(
    *,
    completed: subprocess.CompletedProcess[str],
    last_message_path: Path | None = None,
) -> tuple[dict[str, Any] | None, str, list[str]]:
    last_message: dict[str, Any] | None = None
    output_source = ""
    parse_errors: list[str] = []

    if last_message_path is not None:
        if last_message_path.exists():
            try:
                parsed = json.loads(last_message_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                parse_errors.append(f"last_message_path parse failed: {exc}")
            else:
                if isinstance(parsed, dict):
                    last_message = parsed
                    output_source = "last_message"
                else:
                    parse_errors.append("last_message_path did not contain a JSON object")
        else:
            parse_errors.append(f"last_message_path missing: {last_message_path}")

    if last_message is None:
        try:
            parsed = json.loads(completed.stdout)
        except Exception as exc:  # noqa: BLE001
            parse_errors.append(f"stdout full parse failed: {exc}")
        else:
            if isinstance(parsed, dict):
                last_message = parsed
                output_source = "stdout_full"
            else:
                parse_errors.append("stdout full parse did not contain a JSON object")

    if last_message is None:
        decoder = json.JSONDecoder()
        stdout = completed.stdout
        for start in reversed([index for index, char in enumerate(stdout) if char == "{"]):
            try:
                parsed, end = decoder.raw_decode(stdout[start:])
            except json.JSONDecodeError:
                continue
            if stdout[start + end :].strip():
                continue
            if isinstance(parsed, dict):
                last_message = parsed
                output_source = "stdout_fallback"
                break
        if last_message is None:
            parse_errors.append("stdout fallback could not find a terminal JSON object")

    return last_message, output_source, parse_errors


def _run_codex_step(
    *,
    step_kind: str,
    step: StepConfig,
    runtime: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
    result_path: Path,
    prompt_path: Path,
    schema_path: Path,
    last_message_path: Path,
) -> dict[str, Any]:
    cwd = Path(step.config.get("cwd", runtime["workspace"])).resolve()
    base_prompt = _read_prompt(step, cwd)
    rendered_prompt = render_value(base_prompt, runtime)
    final_prompt = _build_prompt(step_kind, rendered_prompt, runtime)
    write_text(prompt_path, final_prompt)

    schema = RUNNER_RESULT_SCHEMA if step_kind == "run" else VERIFIER_RESULT_SCHEMA
    write_json(schema_path, schema)

    codex_bin = str(step.config.get("codex_bin", "codex"))
    model = str(step.config.get("model", "gpt-5.4"))
    effort = str(step.config.get("reasoning_effort", "medium"))
    sandbox = str(step.config.get("sandbox", "workspace-write"))

    command = [
        codex_bin,
        "exec",
        "--ephemeral",
        "--color",
        "never",
        "-m",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "-s",
        sandbox,
        "-C",
        str(cwd),
        "-o",
        str(last_message_path),
        "--output-schema",
        str(schema_path),
    ]
    if step.config.get("skip_git_repo_check", False):
        command.append("--skip-git-repo-check")
    for add_dir in step.config.get("add_dirs", []):
        command.extend(["--add-dir", str(add_dir)])
    command.append("-")

    env = _step_env(step, runtime)
    timeout_seconds = step.config.get("timeout_seconds")

    rate_limit_retries = int(step.config.get("rate_limit_max_retries", 48))
    rate_limit_max_wait = int(step.config.get("rate_limit_max_wait_seconds", 6 * 60 * 60))

    for rate_limit_retry in range(rate_limit_retries + 1):
        completed = _run_subprocess(
            command=command,
            shell=False,
            cwd=cwd,
            env=env,
            stdin_text=final_prompt,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            timeout_seconds=timeout_seconds,
        )

        if completed.returncode == 0:
            break

        wait_seconds = _rate_limit_wait_seconds(
            stdout=completed.stdout,
            stderr=completed.stderr,
            step=step,
        )
        if wait_seconds is None or rate_limit_retry >= rate_limit_retries:
            payload = {
                "status": "failed",
                "summary": f"codex exec exited with {completed.returncode}",
                "returncode": completed.returncode,
                "command": command,
                "cwd": str(cwd),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "last_message_path": str(last_message_path),
            }
            write_json(result_path, payload)
            return payload

        wait_seconds = max(1, min(wait_seconds, rate_limit_max_wait))
        retry_note = (
            f"\n[spec-loop] rate limit detected; sleeping {wait_seconds}s before retry "
            f"({rate_limit_retry + 1}/{rate_limit_retries})\n"
        )
        write_text(stdout_path, completed.stdout + retry_note)
        write_text(stderr_path, completed.stderr + retry_note)
        time.sleep(wait_seconds)
    else:
        payload = {
            "status": "failed",
            "summary": "codex exec exhausted rate-limit retries",
            "command": command,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "last_message_path": str(last_message_path),
        }
        write_json(result_path, payload)
        return payload

    last_message, output_source, parse_errors = _parse_structured_output(
        completed=completed,
        last_message_path=last_message_path,
    )

    if last_message is None:
        payload = {
            "status": "failed",
            "summary": "could not parse structured codex output",
            "returncode": completed.returncode,
            "command": command,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "last_message_path": str(last_message_path),
            "parse_errors": parse_errors,
        }
        write_json(result_path, payload)
        return payload

    payload = {
        **last_message,
        "returncode": completed.returncode,
        "command": command,
        "cwd": str(cwd),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "prompt_path": str(prompt_path),
        "schema_path": str(schema_path),
        "last_message_path": str(last_message_path),
        "output_source": output_source,
    }
    write_json(result_path, payload)
    return payload


def _run_claude_step(
    *,
    step_kind: str,
    step: StepConfig,
    runtime: dict[str, Any],
    stdout_path: Path,
    stderr_path: Path,
    result_path: Path,
    prompt_path: Path,
    schema_path: Path,
) -> dict[str, Any]:
    cwd = Path(step.config.get("cwd", runtime["workspace"])).resolve()
    base_prompt = _read_prompt(step, cwd)
    rendered_prompt = render_value(base_prompt, runtime)
    final_prompt = _build_prompt(step_kind, rendered_prompt, runtime)
    write_text(prompt_path, final_prompt)

    schema = RUNNER_RESULT_SCHEMA if step_kind == "run" else VERIFIER_RESULT_SCHEMA
    write_json(schema_path, schema)

    claude_bin = str(step.config.get("claude_bin", "claude"))
    model = str(step.config.get("model", "opus"))
    effort = str(step.config.get("reasoning_effort", "medium"))
    permission_mode = str(step.config.get("permission_mode", "bypassPermissions"))

    command = [
        claude_bin,
        "--print",
        "--output-format",
        "json",
        "--model",
        model,
        "--effort",
        effort,
        "--permission-mode",
        permission_mode,
        "--json-schema",
        str(schema_path),
    ]
    if step.config.get("dangerously_skip_permissions", True):
        command.append("--dangerously-skip-permissions")
    for add_dir in step.config.get("add_dirs", []):
        command.extend(["--add-dir", str(add_dir)])
    command.append(final_prompt)

    env = _step_env(step, runtime)
    timeout_seconds = step.config.get("timeout_seconds")

    completed = _run_subprocess(
        command=command,
        shell=False,
        cwd=cwd,
        env=env,
        stdin_text=None,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        timeout_seconds=timeout_seconds,
    )

    if completed.returncode != 0:
        payload = {
            "status": "failed",
            "summary": f"claude exec exited with {completed.returncode}",
            "returncode": completed.returncode,
            "command": command,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }
        write_json(result_path, payload)
        return payload

    last_message, output_source, parse_errors = _parse_structured_output(completed=completed)
    if last_message is None:
        payload = {
            "status": "failed",
            "summary": "could not parse structured claude output",
            "returncode": completed.returncode,
            "command": command,
            "cwd": str(cwd),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "schema_path": str(schema_path),
            "parse_errors": parse_errors,
        }
        write_json(result_path, payload)
        return payload

    payload = {
        **last_message,
        "returncode": completed.returncode,
        "command": command,
        "cwd": str(cwd),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "prompt_path": str(prompt_path),
        "schema_path": str(schema_path),
        "output_source": output_source,
    }
    write_json(result_path, payload)
    return payload


def _run_step(
    *,
    step_kind: str,
    step: StepConfig,
    runtime: dict[str, Any],
    attempt_dir: Path,
) -> dict[str, Any]:
    prefix = "runner" if step_kind == "run" else "verifier"
    stdout_path = attempt_dir / f"{prefix}.stdout.txt"
    stderr_path = attempt_dir / f"{prefix}.stderr.txt"
    result_path = attempt_dir / f"{prefix}.result.json"
    if step.type == "codex_exec":
        prompt_path = attempt_dir / f"{prefix}.prompt.txt"
        schema_path = attempt_dir / f"{prefix}.schema.json"
        last_message_path = attempt_dir / f"{prefix}.last-message.json"
        return _run_codex_step(
            step_kind=step_kind,
            step=step,
            runtime=runtime,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            result_path=result_path,
            prompt_path=prompt_path,
            schema_path=schema_path,
            last_message_path=last_message_path,
        )
    if step.type == "claude_exec":
        prompt_path = attempt_dir / f"{prefix}.prompt.txt"
        schema_path = attempt_dir / f"{prefix}.schema.json"
        return _run_claude_step(
            step_kind=step_kind,
            step=step,
            runtime=runtime,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            result_path=result_path,
            prompt_path=prompt_path,
            schema_path=schema_path,
        )
    return _run_shell_step(
        step_kind=step_kind,
        step=step,
        runtime=runtime,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_path=result_path,
    )


def validate_spec(spec: SpecConfig) -> None:
    if not spec.phases:
        raise SpecLoopError("spec has no phases")
    seen: set[str] = set()
    for phase in spec.phases:
        if phase.id in seen:
            raise SpecLoopError(f"duplicate phase id: {phase.id}")
        seen.add(phase.id)


def run_spec(spec: SpecConfig, *, fresh: bool = False) -> Path:
    validate_spec(spec)
    _ensure_run_root_ignored(spec)
    run_dir = _resolve_run_dir(spec, fresh=fresh)
    run_dir.mkdir(parents=True, exist_ok=True)
    journal_path = run_dir / "journal.jsonl"
    state_path = run_dir / "state.json"
    snapshot_path = run_dir / "spec.snapshot.json"

    if state_path.exists():
        state = _load_state(state_path)
        _journal(journal_path, "resume_run", run_id=run_dir.name)
    else:
        state = _default_state(spec, run_dir.name)
        write_json(snapshot_path, _spec_snapshot(spec))
        _save_state(state_path, state)
        _journal(journal_path, "start_run", run_id=run_dir.name, spec_name=spec.name, workspace=str(spec.workspace))

    for phase in spec.phases:
        phase_dir = run_dir / "phases" / phase.id
        phase_dir.mkdir(parents=True, exist_ok=True)
        phase_state = state["phases"][phase.id]

        if phase_state["status"] == "completed":
            continue
        if phase_state["status"] == "blocked":
            if _needs_verifier_only_retry(phase_state):
                state["status"] = "running"
                _save_state(state_path, state)
            else:
                state["status"] = "blocked"
                _save_state(state_path, state)
                return run_dir

        while True:
            if _needs_verifier_only_retry(phase_state):
                attempt_record = phase_state["attempts"][-1]
                attempt = int(attempt_record["attempt"])
                verifier_retry = int(attempt_record.get("verifier_infra_retries", 0)) + 1
                max_verifier_retries = int(phase.verify.config.get("verifier_infra_max_retries", 3))

                if verifier_retry > max_verifier_retries:
                    phase_state["status"] = "failed"
                    state["status"] = "failed"
                    _journal(
                        journal_path,
                        "phase_failed_verifier_infra_retries",
                        phase_id=phase.id,
                        attempt=attempt,
                        max_retries=max_verifier_retries,
                    )
                    _save_state(state_path, state)
                    return run_dir

                attempt_record["verifier_infra_retries"] = verifier_retry
                attempt_dir = phase_dir / f"attempt-{attempt:02d}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                runtime = _build_context(spec, run_dir, phase, attempt, phase_dir, attempt_dir)
                phase_state["status"] = "verifying"
                state["status"] = "running"
                _save_state(state_path, state)
                _journal(
                    journal_path,
                    "verifier_retry_started",
                    phase_id=phase.id,
                    attempt=attempt,
                    verifier_retry=verifier_retry,
                )

                verifier_payload = _run_step(step_kind="verify", step=phase.verify, runtime=runtime, attempt_dir=attempt_dir)
                write_json(phase_dir / "latest-verifier-result.json", verifier_payload)
                attempt_record["verifier"] = verifier_payload
                _journal(
                    journal_path,
                    "verifier_finished",
                    phase_id=phase.id,
                    attempt=attempt,
                    verifier_retry=verifier_retry,
                    status=verifier_payload.get("status"),
                    summary=verifier_payload.get("summary"),
                )

                verifier_status = verifier_payload.get("status")
                if verifier_status == "passed":
                    phase_state["status"] = "completed"
                    _save_state(state_path, state)
                    break
                if verifier_status == "blocked":
                    phase_state["status"] = "blocked"
                    state["status"] = "blocked"
                    _write_feedback(phase_dir, verifier_payload)
                    _save_state(state_path, state)
                    return run_dir
                if verifier_status == "failed":
                    if _is_codex_auth_failure(verifier_payload):
                        blocked_payload = {
                            **verifier_payload,
                            "status": "blocked",
                            "summary": "codex exec authentication failed; verifier cannot run without Codex/OpenAI auth",
                        }
                        attempt_record["verifier"] = blocked_payload
                        write_json(phase_dir / "latest-verifier-result.json", blocked_payload)
                        _write_feedback(phase_dir, blocked_payload)
                        phase_state["status"] = "blocked"
                        state["status"] = "blocked"
                        _save_state(state_path, state)
                        return run_dir

                    phase_state["status"] = "needs_verification"
                    state["status"] = "running"
                    _save_state(state_path, state)
                    continue

                _write_feedback(phase_dir, verifier_payload)
                phase_state["status"] = "needs_retry"
                _save_state(state_path, state)
                continue

            attempt = len(phase_state["attempts"]) + 1
            if attempt > phase.max_attempts:
                phase_state["status"] = "failed"
                state["status"] = "failed"
                _journal(journal_path, "phase_failed_max_attempts", phase_id=phase.id, max_attempts=phase.max_attempts)
                _save_state(state_path, state)
                return run_dir

            attempt_dir = phase_dir / f"attempt-{attempt:02d}"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            runtime = _build_context(spec, run_dir, phase, attempt, phase_dir, attempt_dir)
            phase_state["status"] = "running"
            state["status"] = "running"
            _save_state(state_path, state)

            _journal(journal_path, "phase_attempt_started", phase_id=phase.id, attempt=attempt)
            git_snapshot = _git_snapshot(spec.workspace)
            git_finalize_retries = int(phase.run.config.get("git_finalize_retries", 2))
            runner_followup_note = ""
            runner_cycle = 0
            while True:
                if runner_followup_note:
                    runtime["runner_followup_note"] = runner_followup_note
                else:
                    runtime.pop("runner_followup_note", None)

                runner_payload = _run_step(step_kind="run", step=phase.run, runtime=runtime, attempt_dir=attempt_dir)
                _journal(
                    journal_path,
                    "runner_cycle_finished",
                    phase_id=phase.id,
                    attempt=attempt,
                    cycle=runner_cycle + 1,
                    status=runner_payload.get("status"),
                    summary=runner_payload.get("summary"),
                )

                if runner_payload.get("status") != "success":
                    break

                git_followup_note = _git_followup_note(spec.workspace, git_snapshot)
                if git_followup_note is None:
                    break

                if runner_cycle >= git_finalize_retries:
                    runner_payload = {
                        **runner_payload,
                        "status": "retry",
                        "summary": "Phase work is close, but git finalization is still incomplete after automatic follow-up.",
                        "notes": git_followup_note,
                    }
                    write_json(attempt_dir / "runner.result.json", runner_payload)
                    break

                runner_cycle += 1
                runner_followup_note = git_followup_note
                _journal(
                    journal_path,
                    "runner_followup_requested",
                    phase_id=phase.id,
                    attempt=attempt,
                    cycle=runner_cycle + 1,
                    summary=git_followup_note,
                )

            write_json(phase_dir / "latest-runner-result.json", runner_payload)
            _journal(
                journal_path,
                "runner_finished",
                phase_id=phase.id,
                attempt=attempt,
                status=runner_payload.get("status"),
                summary=runner_payload.get("summary"),
            )

            attempt_record = {
                "attempt": attempt,
                "started_at": now_utc(),
                "runner": runner_payload,
            }
            phase_state["attempts"].append(attempt_record)

            runner_status = runner_payload.get("status")
            if runner_status == "blocked":
                phase_state["status"] = "blocked"
                state["status"] = "blocked"
                _save_state(state_path, state)
                return run_dir
            if runner_status == "failed":
                phase_state["status"] = "failed"
                state["status"] = "failed"
                _save_state(state_path, state)
                return run_dir
            if runner_status == "retry":
                phase_state["status"] = "needs_retry"
                _save_state(state_path, state)
                continue

            verifier_payload = _run_step(step_kind="verify", step=phase.verify, runtime=runtime, attempt_dir=attempt_dir)
            write_json(phase_dir / "latest-verifier-result.json", verifier_payload)
            _journal(
                journal_path,
                "verifier_finished",
                phase_id=phase.id,
                attempt=attempt,
                status=verifier_payload.get("status"),
                summary=verifier_payload.get("summary"),
            )
            attempt_record["verifier"] = verifier_payload

            verifier_status = verifier_payload.get("status")
            if verifier_status == "passed":
                phase_state["status"] = "completed"
                _save_state(state_path, state)
                break
            if verifier_status == "blocked":
                phase_state["status"] = "blocked"
                state["status"] = "blocked"
                _write_feedback(phase_dir, verifier_payload)
                _save_state(state_path, state)
                return run_dir
            if verifier_status == "failed":
                if _is_codex_auth_failure(verifier_payload):
                    blocked_payload = {
                        **verifier_payload,
                        "status": "blocked",
                        "summary": "codex exec authentication failed; verifier cannot run without Codex/OpenAI auth",
                    }
                    attempt_record["verifier"] = blocked_payload
                    write_json(phase_dir / "latest-verifier-result.json", blocked_payload)
                    _write_feedback(phase_dir, blocked_payload)
                    phase_state["status"] = "blocked"
                    state["status"] = "blocked"
                    _save_state(state_path, state)
                    return run_dir

                phase_state["status"] = "needs_verification"
                state["status"] = "running"
                _save_state(state_path, state)
                continue

            _write_feedback(phase_dir, verifier_payload)
            phase_state["status"] = "needs_retry"
            _save_state(state_path, state)

    state["status"] = "completed"
    _save_state(state_path, state)
    _journal(journal_path, "run_completed", run_id=run_dir.name)
    return run_dir


def print_summary(run_dir: Path) -> None:
    state = _load_state(run_dir / "state.json")
    print(f"Run: {run_dir.name}")
    print(f"Spec: {state['spec_name']}")
    print(f"Workspace: {state['workspace']}")
    print(f"Status: {state['status']}")
    print("")
    for phase_id, phase in state["phases"].items():
        print(f"- {phase_id}: {phase['status']} ({len(phase['attempts'])} attempt(s))")


def exit_code_for_run(run_dir: Path) -> int:
    state = _load_state(run_dir / "state.json")
    status = state["status"]
    if status == "completed":
        return 0
    if status == "blocked":
        return 2
    return 1
