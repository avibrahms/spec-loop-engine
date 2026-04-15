from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

PLAN_PATH = Path('/Users/avi/Documents/Misc/connection-engine/core/system/plans/UNIFIED-MASTER-PLAN.md')
RULES_PATH = Path('/Users/avi/Documents/Misc/connection-engine/core/system/plans/AGENT-EXECUTION-RULES.md')
MASTER_26R_PATH = Path('/Users/avi/Documents/Misc/connection-engine/core/system/plans/phase-26R-strategic-realignment/MASTER-PHASE-26R.md')
WORKSPACE = Path('/Users/avi/Documents/Misc/connection-engine')
SPEC_PATH = Path('/Users/avi/Documents/Projects/spec-loop-engine/specs/connection-engine-post-round7.yaml')
CODEX_BIN = '/Applications/Codex.app/Contents/Resources/codex'
CLAUDE_BIN = '/Users/avi/.local/bin/claude'

START_MARKER = '### ROUND 7A'
STEP_RE = re.compile(r'^\*\*(EXECUTE|VERIFY) Step ([^ ]+) — ([A-Za-z0-9.]+) — ([A-Z]+)\*\*')
PHASE_FILE_RE = re.compile(r'core/system/plans/[^`\s]+\.md')

CODEX_EFFORT = {'LOW': 'low', 'MED': 'medium', 'HIGH': 'high', 'MAX': 'xhigh'}
CLAUDE_EFFORT = {'LOW': 'low', 'MED': 'medium', 'HIGH': 'high', 'MAX': 'max'}


def literal_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    style = '|' if '\n' in data else None
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style=style)


yaml.SafeDumper.add_representer(str, literal_representer)


def slugify_step(step: str) -> str:
    return step.lower().replace('.', '-').replace('/', '-')


def nice_title(path: str) -> str:
    stem = Path(path).stem
    stem = re.sub(r'^PHASE-', '', stem)
    return ' '.join(part.capitalize() if not part.isupper() else part for part in stem.split('-'))


def parse_steps() -> list[dict[str, Any]]:
    text = PLAN_PATH.read_text(encoding='utf-8')
    idx = text.index(START_MARKER)
    lines = text[idx:].splitlines()
    items: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        match = STEP_RE.match(lines[i].strip())
        if not match:
            i += 1
            continue
        kind, step_id, model_label, effort = match.groups()
        i += 1
        while i < len(lines) and lines[i].strip() != '```':
            i += 1
        if i >= len(lines):
            raise RuntimeError(f'missing code fence for step {step_id} {kind}')
        i += 1
        block: list[str] = []
        while i < len(lines) and lines[i].strip() != '```':
            block.append(lines[i])
            i += 1
        prompt = '\n'.join(block).strip() + '\n'
        phase_matches = PHASE_FILE_RE.findall(prompt)
        if not phase_matches:
            raise RuntimeError(f'could not find phase file in {step_id} {kind}')
        phase_file = str(WORKSPACE / phase_matches[-1])
        items.append(
            {
                'kind': kind.lower(),
                'step_id': step_id,
                'model_label': model_label,
                'effort': effort,
                'prompt': prompt,
                'phase_file': phase_file,
            }
        )
        i += 1
    return items


def build_run_prompt(step_id: str, prompt: str) -> str:
    extra = [
        'Spec-loop constraints for this run:',
        '- Stay on the current git branch for the entire phase.',
        '- Never create, switch, rename, delete, or tag branches.',
        '- Never create a git worktree.',
        '- If the phase file mentions branch names, tags, or checkout instructions, treat them as overridden by this spec and continue on the current branch.',
        '- Do the implementation, execution, testing, and verification work the phase requires inside the workspace.',
        '- If prior verifier feedback exists, fix every issue before doing any net-new work.',
        '- Stop only when this phase is genuinely ready for independent verification or is externally blocked.',
    ]
    if step_id == '15' or re.match(r'^(1[6-9]|2[0-9]|3[0-7]|27\.5a|28\.5a)$', step_id):
        extra.append('- Treat completed 26R outputs as frozen inputs. Consume them; do not reopen their strategic decisions.')
    if step_id == '15':
        extra.append('- Explicitly consume the 26R outputs such as `core/system/data/pricing-matrix.yaml`, `api-surface.yaml`, `open-source-classification.yaml`, and `modules/system-zero/distribution/` where relevant instead of re-deriving them.')
    return prompt.rstrip() + '\n\n' + '\n'.join(extra) + '\n'


def build_verify_prompt(prompt: str) -> str:
    extra = [
        'Structured output requirements for this spec runner:',
        '- Do not modify files.',
        '- If you find issues, convert the FIX BLOCK into structured JSON.',
        '- Put each numbered FIX BLOCK item into `issues[]` as one self-contained remediation item with exact path, what is wrong, and the exact fix needed.',
        '- Put the consolidated fix summary in `repair_hint`.',
        '- Return `passed` only when the phase is truly clean enough for the next dependent phase to begin.',
    ]
    return prompt.rstrip() + '\n\n' + '\n'.join(extra) + '\n'


def build_step_config(kind: str, model_label: str, effort: str) -> dict[str, Any]:
    if model_label.upper() == 'GPT':
        return {
            'type': 'codex_exec',
            'codex_bin': CODEX_BIN,
            'model': 'gpt-5.4',
            'reasoning_effort': CODEX_EFFORT[effort],
            'sandbox': 'read-only' if kind == 'verify' else 'danger-full-access',
            'skip_git_repo_check': True,
            'add_dirs': [str(WORKSPACE)],
        }
    return {
        'type': 'claude_exec',
        'claude_bin': CLAUDE_BIN,
        'model': 'opus',
        'reasoning_effort': CLAUDE_EFFORT[effort],
        'permission_mode': 'bypassPermissions',
        'dangerously_skip_permissions': True,
        'add_dirs': [str(WORKSPACE)],
    }


def build_phases(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    paired: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        step_id = item['step_id']
        if step_id not in paired:
            paired[step_id] = {}
            order.append(step_id)
        paired[step_id][item['kind']] = item

    phases: list[dict[str, Any]] = []
    for step_id in order:
        execute = paired[step_id].get('execute')
        verify = paired[step_id].get('verify')
        if not execute or not verify:
            continue
        phase_file = execute['phase_file']
        phases.append(
            {
                'id': f'step-{slugify_step(step_id)}',
                'title': f'Step {step_id} - {nice_title(phase_file)}',
                'max_attempts': 4,
                'vars': {
                    'step_number': step_id,
                    'phase_file': phase_file,
                },
                'run': {
                    **build_step_config('run', execute['model_label'], execute['effort']),
                    'prompt': build_run_prompt(step_id, execute['prompt']),
                },
                'verify': {
                    **build_step_config('verify', verify['model_label'], verify['effort']),
                    'prompt': build_verify_prompt(verify['prompt']),
                },
            }
        )

    gate_prompt = (
        f'Read `{MASTER_26R_PATH}` and evaluate the "Verification Gate (single bar before proceeding to Round 8)" checklist against the current repo state. '
        'All 6 bullets must pass before Round 8 can begin. '
        'If any bullet fails, return retry with one issue per failing bullet and the exact file or subsystem that must be fixed. '
        'Stay on the current git branch and do not create tags, branches, or worktrees.'
    )
    phases.insert(
        9,
        {
            'id': 'step-14r-gate',
            'title': 'Step 14R Gate - Verification Gate',
            'max_attempts': 3,
            'vars': {'step_number': '14R.GATE', 'phase_file': str(MASTER_26R_PATH)},
            'run': {
                **build_step_config('run', 'GPT', 'MED'),
                'prompt': gate_prompt,
            },
            'verify': {
                **build_step_config('verify', 'Opus', 'LOW'),
                'prompt': build_verify_prompt(gate_prompt),
            },
        },
    )
    return phases


def main() -> None:
    items = parse_steps()
    spec = {
        'version': 1,
        'name': 'connection-engine-post-round7',
        'workspace': str(WORKSPACE),
        'run_root': str(WORKSPACE / '.spec-loop' / 'runs' / 'connection-engine-post-round7'),
        'vars': {
            'rules_path': str(RULES_PATH),
            'master_26r_path': str(MASTER_26R_PATH),
        },
        'defaults': {
            'max_attempts': 4,
        },
        'phases': build_phases(items),
    }
    SPEC_PATH.write_text(yaml.safe_dump(spec, sort_keys=False, width=1000), encoding='utf-8')
    print(SPEC_PATH)


if __name__ == '__main__':
    main()
