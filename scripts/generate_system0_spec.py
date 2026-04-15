from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path("/Users/avi/Documents/Projects/spec-loop-engine")
PLAN_ROOT = Path("/Users/avi/Documents/Projects/system0-natural")
OUTPUT_PATH = PROJECT_ROOT / "specs" / "system0-plan.yaml"
CODEX_BIN = "/Applications/Codex.app/Contents/Resources/codex"
VERIFIER_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Users/avi/.local/bin:/Applications/Codex.app/Contents/Resources"

PHASE_ORDER = [
    "phase-00-prerequisites",
    "phase-01-protocol-spec",
    "phase-02-sz-cli",
    "phase-03-universal-interfaces",
    "phase-04-reconciliation-engine",
    "phase-05-host-adapters",
    "phase-06-absorb-workflow",
    "phase-07-repo-genesis",
    "phase-08-port-modules",
    "phase-09-catalog-and-distribution",
    "phase-10-cloud-and-billing",
    "phase-11-website",
    "phase-12-test-static-template",
    "phase-13-test-dynamic-template",
    "phase-14-test-absorb-os-feature",
    "phase-15-launch",
    "phase-16-reconstruct-connection-engine",
]


EXECUTE_EFFORTS = {
    "phase-00-prerequisites": "medium",
    "phase-01-protocol-spec": "high",
    "phase-02-sz-cli": "high",
    "phase-03-universal-interfaces": "high",
    "phase-04-reconciliation-engine": "high",
    "phase-05-host-adapters": "high",
    "phase-06-absorb-workflow": "xhigh",
    "phase-07-repo-genesis": "xhigh",
    "phase-08-port-modules": "high",
    "phase-09-catalog-and-distribution": "high",
    "phase-10-cloud-and-billing": "xhigh",
    "phase-11-website": "high",
    "phase-12-test-static-template": "high",
    "phase-13-test-dynamic-template": "high",
    "phase-14-test-absorb-os-feature": "xhigh",
    "phase-15-launch": "high",
    "phase-16-reconstruct-connection-engine": "xhigh",
}


VERIFY_EFFORTS = {
    "phase-00-prerequisites": "low",
    "phase-01-protocol-spec": "medium",
    "phase-02-sz-cli": "medium",
    "phase-03-universal-interfaces": "medium",
    "phase-04-reconciliation-engine": "medium",
    "phase-05-host-adapters": "medium",
    "phase-06-absorb-workflow": "high",
    "phase-07-repo-genesis": "high",
    "phase-08-port-modules": "medium",
    "phase-09-catalog-and-distribution": "medium",
    "phase-10-cloud-and-billing": "high",
    "phase-11-website": "medium",
    "phase-12-test-static-template": "medium",
    "phase-13-test-dynamic-template": "medium",
    "phase-14-test-absorb-os-feature": "high",
    "phase-15-launch": "medium",
    "phase-16-reconstruct-connection-engine": "high",
}


def phase_title(phase_dir_name: str) -> str:
    name = phase_dir_name.split("-", 2)[-1]
    return name.replace("-", " ").title()


def executor_prompt(plan_path: str, title: str) -> str:
    return f"""Execute the System Zero phase defined in `{plan_path}`.

Treat these files as the source of truth before you change anything:
- `{PLAN_ROOT}/plan/README.md`
- `{PLAN_ROOT}/plan/EXECUTION_RULES.md`
- `{PLAN_ROOT}/plan/PROTOCOL_SPEC.md`
- `{PLAN_ROOT}/plan/ARCHITECTURE.md`
- `{plan_path}`

Phase title: {title}

Required behavior:
- Follow the phase plan literally.
- Canonical git policy for this spec: current-branch checkpoint mode.
- Never create, switch, rename, or delete git branches.
- Stay on the current branch for the entire run.
- If a phase plan asks for branch operations, merge steps, or exact one-commit branch rules, treat that language as overridden by this spec and continue in current-branch checkpoint mode instead.
- Perform the actual implementation, execution, testing, and verification the phase requires.
- If prior verifier feedback exists, read it first and fix every issue before doing anything else.
- Do not return success while the git worktree is dirty, while uncommitted or untracked phase residue remains, or while the current-branch checkpoint commit(s) for this phase are still missing.
- Stop only when this phase is genuinely ready for independent verification, or when an external blocker prevents progress.
- Do not drift into later phases.

In `summary`, say what is now complete.
In `artifacts`, list the most important files changed.
In `resume_hint`, give the exact next move if another executor has to continue from here.
"""


def verifier_prompt(plan_path: str, title: str) -> str:
    return f"""Audit the just-finished System Zero phase defined in `{plan_path}`.

Treat these files as the source of truth:
- `{PLAN_ROOT}/plan/README.md`
- `{PLAN_ROOT}/plan/EXECUTION_RULES.md`
- `{PLAN_ROOT}/plan/PROTOCOL_SPEC.md`
- `{PLAN_ROOT}/plan/ARCHITECTURE.md`
- `{plan_path}`

Phase title: {title}

Verification requirements:
- Independently verify the acceptance criteria and the concrete outputs promised by the phase plan.
- Canonical git policy for this spec: current-branch checkpoint mode.
- Treat any git branch creation, switching, renaming, or deletion as a failure against this spec.
- Do not require phase branches, phase merges, or exact one-commit branch counts even if stale plan text still mentions them.
- Require a clean current branch with the phase checkpoint commit(s) already recorded and no git residue left for a cleanup attempt.
- Check for hidden corruption, partial execution, unverified claims, missed follow-through, and interruption residue.
- Check for obvious regressions introduced into earlier completed work.
- Return `passed` only if the phase is actually clean and complete enough to let the next phase begin.
- Return `retry` with concrete issues if anything remains to fix or verify.
- Return `blocked` only for external blockers.
"""


def build_spec() -> dict:
    phase_dirs = []
    for phase_name in PHASE_ORDER:
        phase_dir = PLAN_ROOT / "plan" / phase_name
        if not (phase_dir / "PLAN.md").exists():
            raise FileNotFoundError(f"Missing PLAN.md for {phase_name}")
        phase_dirs.append(phase_dir)

    phases = []
    for phase_dir in phase_dirs:
        plan_path = phase_dir / "PLAN.md"
        phases.append(
            {
                "id": phase_dir.name,
                "title": phase_title(phase_dir.name),
                "vars": {
                    "phase_plan": str(plan_path),
                },
                "run": {
                    "reasoning_effort": EXECUTE_EFFORTS.get(phase_dir.name, "high"),
                    "prompt": executor_prompt(str(plan_path), phase_title(phase_dir.name)),
                },
                "verify": {
                    "reasoning_effort": VERIFY_EFFORTS.get(phase_dir.name, "medium"),
                    "prompt": verifier_prompt(str(plan_path), phase_title(phase_dir.name)),
                },
            }
        )

    return {
        "version": 1,
        "name": "system0-plan",
        "workspace": str(PLAN_ROOT),
        "vars": {
            "model": "gpt-5.4",
            "execute_effort": "high",
            "verify_effort": "medium",
            "max_attempts": 5,
        },
        "defaults": {
            "max_attempts": "${max_attempts}",
            "runner": {
                "type": "codex_exec",
                "codex_bin": CODEX_BIN,
                "model": "${model}",
                "reasoning_effort": "${execute_effort}",
                "sandbox": "danger-full-access",
                "skip_git_repo_check": True,
                "git_finalize_retries": 2,
                "add_dirs": [str(PLAN_ROOT)],
            },
            "verifier": {
                "type": "codex_exec",
                "codex_bin": CODEX_BIN,
                "model": "${model}",
                "reasoning_effort": "${verify_effort}",
                "sandbox": "workspace-write",
                "skip_git_repo_check": True,
                "env": {
                    "PATH": VERIFIER_PATH,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "TMPDIR": "${attempt_dir}/verifier-tmp",
                    "TMP": "${attempt_dir}/verifier-tmp",
                    "TEMP": "${attempt_dir}/verifier-tmp",
                    "XDG_CACHE_HOME": "${attempt_dir}/verifier-cache",
                },
                "add_dirs": [str(PLAN_ROOT)],
            },
        },
        "phases": phases,
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(yaml.safe_dump(build_spec(), sort_keys=False), encoding="utf-8")
    print(OUTPUT_PATH)


if __name__ == "__main__":
    main()
