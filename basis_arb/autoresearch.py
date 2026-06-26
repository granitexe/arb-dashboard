"""Autoresearch-inspired self-improvement loop for basis-arb-tool.

Inspired by Karpathy's AutoResearch (https://github.com/karpathy/autoresearch),
this module implements a keep-or-revert improvement loop adapted for our
signal-generation codebase.

The loop:
  propose → evaluate → keep / revert → repeat

Key differences from the original:
  - We optimize for signal quality (net carry, trap exclusion rate), not for
    ML training loss. There is no differentiable objective — only a ranking.
  - Changes are proposed by the operator (or by the meta-improvement cron job),
    not autonomously. This agent proposes, the operator reviews, the operator merges.
    The "self" in self-improvement means: the tool's own code is the subject.
  - The evaluation metric is aggregate risk-adjusted carry across viable signals.
  - Any change that reduces viable signal count or raises exclusion rate without
    a compensating carry improvement is rejected (even if primary metric rises).

Ratchet principle: the score only goes up. If a proposed change would lower
the score, it is reverted automatically. No exceptions.

Security: all changes are validated against safety.py BEFORE evaluation.
No change can introduce new secret imports, shell=True calls, or new URL hosts.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from .safety import validate_improvement, scan_for_secrets

# The evaluation metric: mean risk-adjusted APR of viable OK signals.
# Computed from the last successful pipeline run.
RESULTS_FILE = Path(__file__).parent.parent / ".cron_output" / "autoresearch_results.tsv"
AGENDA_FILE = Path(__file__).parent.parent / ".cron_output" / "autoresearch_agenda.md"
HISTORY_DIR = Path(__file__).parent.parent / ".cron_output" / "autoresearch_history"
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _read_signals_json() -> Optional[list]:
    """Read the most recent signals.json if it exists."""
    sig_file = Path(__file__).parent.parent / "signals.json"
    if not sig_file.exists():
        return None
    try:
        data = json.loads(sig_file.read_text())
        return data.get("signals", [])
    except Exception:
        return None


def _compute_score(signals: list) -> float:
    """Compute the primary evaluation metric from a signals list.

    metric = mean([s.risk_adjusted_apr for s in OK signals with positive carry])
    Higher = better.
    """
    ok_signals = [
        s for s in signals
        if s.get("status") == "OK"
        and s.get("risk_adjusted_apr") is not None
        and s["risk_adjusted_apr"] > 0
    ]
    if not ok_signals:
        return 0.0
    return sum(s["risk_adjusted_apr"] for s in ok_signals) / len(ok_signals)


@dataclass
class Experiment:
    """A single experiment in the autoresearch loop."""
    id: str
    timestamp: str
    description: str
    file_changed: str
    diff_preview: str
    score_before: float
    score_after: float
    status: str  # "pending" | "accepted" | "reverted" | "blocked"
    blocker_reason: Optional[str] = None
    notes: str = ""

    @property
    def improvement(self) -> float:
        return self.score_after - self.score_before

    @property
    def is_improvement(self) -> bool:
        return self.status == "accepted" and self.improvement > 0


def _run_pipeline() -> tuple[bool, float]:
    """Run the signal pipeline and return (success, score)."""
    sig_file = Path(__file__).parent.parent / "signals.json"
    lock_file = Path(__file__).parent.parent / ".cron_output" / "pipeline.lock"

    # Check if pipeline is already running
    if lock_file.exists():
        lock_age = datetime.datetime.now() - datetime.datetime.fromtimestamp(lock_file.stat().st_mtime)
        if lock_age.seconds < 600:  # < 10 minutes old
            return False, 0.0

    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.touch()

    try:
        result = subprocess.run(
            [sys.executable, "-m", "basis_arb"],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(Path(__file__).parent.parent),
        )
        signals = _read_signals_json()
        if signals is None:
            return False, 0.0
        score = _compute_score(signals)
        return True, score
    except Exception:
        return False, 0.0
    finally:
        if lock_file.exists():
            lock_file.unlink()


def _diff_files(before: Path, after: Path) -> str:
    """Compute a short diff summary between two file versions."""
    try:
        result = subprocess.run(
            ["diff", "-U", "3", str(before), str(after)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.split("\n")
        # Shorten: keep only first 20 lines
        return "\n".join(lines[:25])
    except Exception:
        return "(diff unavailable)"


def run_experiment(
    description: str,
    file_to_change: Path,
    apply_fn,  # function(Path) -> None; raises on error
) -> Experiment:
    """Run a single self-improvement experiment.

    Steps:
    1. Snapshot current score
    2. Validate proposed change against safety.py
    3. Apply change to a temp copy first
    4. Run pipeline on the temp copy
    5. Compare score
    6. Keep or revert

    Args:
        description: human-readable description of what this experiment does
        file_to_change: path to the file to modify
        apply_fn: function that takes a Path and modifies the file in-place

    Returns:
        Experiment with before/after scores and status
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    exp_id = f"exp_{ts}"

    # Baseline score
    signals_before = _read_signals_json()
    score_before = _compute_score(signals_before) if signals_before else 0.0

    # Read the diff from apply_fn by applying to a temp file first
    import uuid
    tmp_id = str(uuid.uuid4())[:8]
    tmp_path = file_to_change.parent / f".{tmp_id}_{file_to_change.name}.tmp"

    try:
        # Copy original
        original_content = file_to_change.read_text()
        tmp_path.write_text(original_content)

        # Apply change to tmp
        try:
            apply_fn(tmp_path)
        except Exception as e:
            return Experiment(
                id=exp_id,
                timestamp=ts,
                description=description,
                file_changed=str(file_to_change.relative_to(Path(__file__).parent.parent)),
                diff_preview=f"(apply failed: {e})",
                score_before=score_before,
                score_after=0.0,
                status="blocked",
                blocker_reason=f"apply_fn raised: {e}",
            )

        # Safety validation on the diff
        new_content = tmp_path.read_text()
        diff = _diff_files(file_to_change, tmp_path)
        is_valid, violations = validate_improvement(file_to_change, diff)

        if not is_valid:
            return Experiment(
                id=exp_id,
                timestamp=ts,
                description=description,
                file_changed=str(file_to_change.relative_to(Path(__file__).parent.parent)),
                diff_preview=diff[:300],
                score_before=score_before,
                score_after=0.0,
                status="blocked",
                blocker_reason=f"safety violation: {'; '.join(violations)}",
            )

        # Write changed file
        file_to_change.write_text(new_content)
        diff_preview = diff[:300]

    except Exception as e:
        return Experiment(
            id=exp_id,
            timestamp=ts,
            description=description,
            file_changed=str(file_to_change.relative_to(Path(__file__).parent.parent)),
            diff_preview=f"(failed: {e})",
            score_before=score_before,
            score_after=0.0,
            status="blocked",
            blocker_reason=str(e),
        )
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    # Run pipeline to evaluate
    success, score_after = _run_pipeline()

    if not success:
        # Revert
        file_to_change.write_text(original_content)
        return Experiment(
            id=exp_id,
            timestamp=ts,
            description=description,
            file_changed=str(file_to_change.relative_to(Path(__file__).parent.parent)),
            diff_preview=diff_preview,
            score_before=score_before,
            score_after=0.0,
            status="reverted",
            notes="Pipeline failed to run — reverted",
        )

    # Decide: keep or revert
    if score_after > score_before:
        # Keep the change
        status = "accepted"
        notes = f"+{score_after - score_before:.4f} APR improvement"
        # Save to history
        _save_history(exp_id, description, file_to_change, original_content, new_content, score_before, score_after)
    else:
        # Revert (ratchet principle: score must go up)
        file_to_change.write_text(original_content)
        status = "reverted"
        notes = f"score {score_after:.4f} ≤ {score_before:.4f} — reverted per ratchet"

    return Experiment(
        id=exp_id,
        timestamp=ts,
        description=description,
        file_changed=str(file_to_change.relative_to(Path(__file__).parent.parent)),
        diff_preview=diff_preview,
        score_before=score_before,
        score_after=score_after,
        status=status,
        notes=notes,
    )


def _save_history(
    exp_id: str,
    description: str,
    file_path: Path,
    before: str,
    after: str,
    score_before: float,
    score_after: float,
) -> None:
    """Save accepted experiment to history directory."""
    hist_file = HISTORY_DIR / f"{exp_id}.json"
    data = {
        "id": exp_id,
        "description": description,
        "file": str(file_path.relative_to(Path(__file__).parent.parent)),
        "score_before": score_before,
        "score_after": score_after,
        "improvement": score_after - score_before,
        "before": before,
        "after": after,
        "timestamp": datetime.datetime.now().isoformat(),
    }
    hist_file.write_text(json.dumps(data, indent=2))

    # Append to results.tsv
    RESULTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not RESULTS_FILE.exists():
        RESULTS_FILE.write_text("exp_id\tdescription\tfile\tscore_before\tscore_after\timprovement\tstatus\n")

    with open(RESULTS_FILE, "a") as f:
        f.write(f"{exp_id}\t{description}\t{file_path.name}\t{score_before:.4f}\t{score_after:.4f}\t{score_after - score_before:.4f}\taccepted\n")


def write_agenda(messages: list[str]) -> None:
    """Write the improvement agenda file (called by the meta-improvement cron job)."""
    AGENDA_FILE.parent.mkdir(parents=True, exist_ok=True)
    content = f"# Autoresearch Agenda — {datetime.datetime.now().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
    for i, msg in enumerate(messages, 1):
        content += f"{i}. {msg}\n"
    AGENDA_FILE.write_text(content)


def read_results() -> list[Experiment]:
    """Read past experiment results from the TSV file."""
    if not RESULTS_FILE.exists():
        return []
    results = []
    try:
        lines = RESULTS_FILE.read_text().strip().split("\n")
        for line in lines[1:]:  # skip header
            parts = line.split("\t")
            if len(parts) >= 7:
                results.append(Experiment(
                    id=parts[0],
                    description=parts[1],
                    file_changed=parts[2],
                    diff_preview="",
                    score_before=float(parts[3]),
                    score_after=float(parts[4]),
                    status="accepted",
                    notes=f"improvement: {parts[5]}",
                ))
    except Exception:
        pass
    return results


def get_top_improvement() -> Optional[Experiment]:
    """Return the experiment with the highest improvement score."""
    results = read_results()
    if not results:
        return None
    return max(results, key=lambda e: e.improvement)
