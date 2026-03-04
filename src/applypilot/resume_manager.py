"""Resume management: load, select, validate per-job resume selection.

Includes conflict queue management and a learning system that recommends
resumes for jobs based on past manual selections.
"""

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from applypilot.config import get_resume_path, load_resumes_manifest, RESUMES_MANIFEST_PATH

log = logging.getLogger(__name__)


def get_job_resume_id(job: dict, default_id: str = "default") -> str:
    """Get the resume ID to use for a job.

    Selection priority:
    1. Job's explicit override (resume_id if selection_method == 'override')
    2. Job title pattern match (if job title matches resume keywords)
    3. Job's assigned resume_id (if auto-assigned to this job)
    4. User's default resume

    Args:
        job: Job dict with possible title, resume_id, resume_selection_method fields.
        default_id: Default resume ID to fall back to.

    Returns:
        Resume ID to use for this job.
    """
    # 1. Explicit override always wins
    if job.get("resume_selection_method") == "override":
        return job.get("resume_id", default_id)

    # 2. Try job title pattern matching
    job_title = (job.get("title") or "").lower()
    if job_title:
        matched_id = _match_job_to_resume(job_title)
        if matched_id:
            return matched_id

    # 3. Use job's assigned resume_id (if not from pattern match above)
    if job.get("resume_id") and job.get("resume_selection_method") != "override":
        return job["resume_id"]

    # 4. Fall back to default
    return default_id


def get_job_resume_path(job: dict) -> Path:
    """Get the resume file path for a job.

    Args:
        job: Job dict with resume_id field.

    Returns:
        Path to the resume text file.
    """
    resume_id = get_job_resume_id(job)
    return get_resume_path(resume_id)


def assign_resume_to_job(
    conn: sqlite3.Connection,
    url: str,
    resume_id: str,
    override: bool = False,
) -> None:
    """Assign a resume to a job.

    Args:
        conn: Database connection.
        url: Job URL (primary key).
        resume_id: Resume ID to assign.
        override: If True, marks this as a manual override. If False, auto-assignment.
    """
    method = "override" if override else "auto"
    conn.execute(
        "UPDATE jobs SET resume_id=?, resume_selection_method=? WHERE url=?",
        (resume_id, method, url),
    )
    conn.commit()


def validate_resume_exists(resume_id: str) -> bool:
    """Check if a resume exists.

    Args:
        resume_id: Resume ID to validate.

    Returns:
        True if the resume file exists.
    """
    path = get_resume_path(resume_id)
    return path.exists()


def get_default_resume_id() -> str:
    """Get the default resume ID from manifest.

    Returns:
        The default resume ID, or 'default' if not configured.
    """
    manifest = load_resumes_manifest()
    return manifest.get("default_resume_id", "default")


def set_default_resume_id(resume_id: str) -> None:
    """Set the default resume ID in manifest.

    Args:
        resume_id: Resume ID to set as default.
    """
    import json
    from applypilot.config import RESUMES_MANIFEST_PATH

    manifest = load_resumes_manifest()
    manifest["default_resume_id"] = resume_id
    RESUMES_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _match_job_to_resume(job_title: str) -> str | None:
    """Check if job title matches any resume's job title keywords.

    Matches against resume's job_title_keywords field (space or comma separated).
    Uses specificity-based matching: longer keyword matches win.

    Args:
        job_title: Job title to match (lowercase).

    Returns:
        Resume ID if a match is found, None otherwise.
    """
    manifest = load_resumes_manifest()
    resumes = manifest.get("resumes", [])

    best_match = None
    best_match_length = 0

    for resume in resumes:
        keywords = resume.get("job_title_keywords", [])
        if not keywords:
            continue

        # Normalize keywords to list of lowercase strings
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace(",", " ").split() if k.strip()]

        # Check if any keyword appears in job title
        for keyword in keywords:
            keyword_lower = keyword.lower()
            if keyword_lower in job_title:
                # Longer matches are more specific and win
                if len(keyword_lower) > best_match_length:
                    best_match = resume.get("id")
                    best_match_length = len(keyword_lower)

    return best_match


def set_job_title_keywords(resume_id: str, keywords: list[str] | str) -> None:
    """Set job title keywords that trigger this resume.

    Args:
        resume_id: Resume ID to configure.
        keywords: List of keywords or space/comma-separated string.
                  E.g., ["python", "backend"] or "python backend" or "python, backend"
    """
    import json
    from applypilot.config import RESUMES_MANIFEST_PATH

    # Normalize keywords to list
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.replace(",", " ").split() if k.strip()]

    manifest = load_resumes_manifest()
    resumes = manifest.get("resumes", [])

    # Find and update the resume
    for resume in resumes:
        if resume.get("id") == resume_id:
            resume["job_title_keywords"] = keywords
            break

    manifest["resumes"] = resumes
    RESUMES_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def get_job_title_keywords(resume_id: str) -> list[str]:
    """Get job title keywords for a resume.

    Args:
        resume_id: Resume ID to query.

    Returns:
        List of job title keywords, or empty list if none configured.
    """
    manifest = load_resumes_manifest()
    resumes = manifest.get("resumes", [])

    for resume in resumes:
        if resume.get("id") == resume_id:
            return resume.get("job_title_keywords", [])

    return []


def get_matching_resumes(job_title: str) -> list[dict]:
    """Get all resumes that match a job title, sorted by specificity.

    Useful for detecting conflicts or showing what matches a job.

    Args:
        job_title: Job title to match (case-insensitive).

    Returns:
        List of dicts: [{"resume_id": "...", "name": "...", "matched_keyword": "...", "keyword_length": ...}]
        Sorted by keyword length (longest/most specific first).
    """
    manifest = load_resumes_manifest()
    resumes = manifest.get("resumes", [])
    matches = []

    job_title_lower = job_title.lower()

    for resume in resumes:
        keywords = resume.get("job_title_keywords", [])
        if not keywords:
            continue

        # Normalize keywords to list of lowercase strings
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.replace(",", " ").split() if k.strip()]

        # Find matching keywords
        for keyword in keywords:
            keyword_lower = keyword.lower()
            if keyword_lower in job_title_lower:
                matches.append({
                    "resume_id": resume.get("id"),
                    "name": resume.get("name"),
                    "matched_keyword": keyword,
                    "keyword_length": len(keyword_lower),
                })
                break  # Only count first matching keyword per resume

    # Sort by keyword length (longest/most specific first)
    matches.sort(key=lambda m: m["keyword_length"], reverse=True)

    return matches


# ---------------------------------------------------------------------------
# Conflict Queue Management
# ---------------------------------------------------------------------------

def has_resume_conflict(job_title: str) -> bool:
    """Check if a job title triggers a resume conflict (2+ resumes match)."""
    return len(get_matching_resumes(job_title)) >= 2


def queue_conflict(
    conn: sqlite3.Connection,
    url: str,
    job_title: str,
    matches: list[dict],
) -> None:
    """Add a job to the conflict queue for user review.

    Args:
        conn: Database connection.
        url: Job URL (primary key).
        job_title: Job title text.
        matches: List of matching resume dicts from get_matching_resumes().
    """
    now = datetime.now(timezone.utc).isoformat()

    # Get recommendation from learning system
    rec_id, rec_confidence = get_recommendation(conn, job_title)

    conn.execute(
        """INSERT OR REPLACE INTO resume_conflict_queue
           (url, job_title, matches_json, match_count, queued_at,
            recommendation_id, recommendation_confidence)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (url, job_title, json.dumps(matches), len(matches), now,
         rec_id, rec_confidence),
    )
    conn.commit()
    log.info("Queued resume conflict for '%s' (%d matches)", job_title[:40], len(matches))


def resolve_conflict(
    conn: sqlite3.Connection,
    url: str,
    chosen_resume_id: str,
) -> None:
    """User resolves a conflict by picking a resume.

    This also records the selection for future learning.

    Args:
        conn: Database connection.
        url: Job URL.
        chosen_resume_id: Resume ID the user selected.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Get the conflict record for learning data
    row = conn.execute(
        "SELECT job_title, matches_json FROM resume_conflict_queue WHERE url=?",
        (url,),
    ).fetchone()

    if not row:
        return

    job_title = row[0]
    matches = json.loads(row[1]) if row[1] else []

    # Find matched keyword for the chosen resume
    matched_keyword = ""
    for m in matches:
        if m["resume_id"] == chosen_resume_id:
            matched_keyword = m.get("matched_keyword", "")
            break

    # Mark conflict as resolved
    conn.execute(
        """UPDATE resume_conflict_queue
           SET resolved_at=?, chosen_resume_id=?
           WHERE url=?""",
        (now, chosen_resume_id, url),
    )

    # Record selection for learning
    conn.execute(
        """INSERT INTO resume_selections
           (job_url, job_title, chosen_resume_id, matched_keyword,
            all_matches_json, selected_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (url, job_title, chosen_resume_id, matched_keyword,
         json.dumps(matches), now),
    )

    # Assign the chosen resume to the job (as override)
    assign_resume_to_job(conn, url, chosen_resume_id, override=True)

    conn.commit()
    log.info("Resolved conflict for '%s' → %s", job_title[:40], chosen_resume_id)


def get_pending_conflicts(conn: sqlite3.Connection) -> list[dict]:
    """Get all unresolved conflicts in the queue.

    Returns:
        List of dicts with url, job_title, matches, match_count,
        queued_at, recommendation_id, recommendation_confidence.
    """
    rows = conn.execute(
        """SELECT url, job_title, matches_json, match_count, queued_at,
                  recommendation_id, recommendation_confidence
           FROM resume_conflict_queue
           WHERE resolved_at IS NULL
           ORDER BY queued_at DESC"""
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "url": row[0],
            "job_title": row[1],
            "matches": json.loads(row[2]) if row[2] else [],
            "match_count": row[3],
            "queued_at": row[4],
            "recommendation_id": row[5],
            "recommendation_confidence": row[6] or 0.0,
        })
    return results


def get_resolved_conflicts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Get recently resolved conflicts (for review/history)."""
    rows = conn.execute(
        """SELECT url, job_title, matches_json, match_count, queued_at,
                  resolved_at, chosen_resume_id,
                  recommendation_id, recommendation_confidence
           FROM resume_conflict_queue
           WHERE resolved_at IS NOT NULL
           ORDER BY resolved_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    results = []
    for row in rows:
        results.append({
            "url": row[0],
            "job_title": row[1],
            "matches": json.loads(row[2]) if row[2] else [],
            "match_count": row[3],
            "queued_at": row[4],
            "resolved_at": row[5],
            "chosen_resume_id": row[6],
            "recommendation_id": row[7],
            "recommendation_confidence": row[8] or 0.0,
        })
    return results


def is_conflict_queued(conn: sqlite3.Connection, url: str) -> bool:
    """Check if a job is already in the conflict queue (pending or resolved)."""
    row = conn.execute(
        "SELECT 1 FROM resume_conflict_queue WHERE url=?", (url,)
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Learning / Recommendation System
# ---------------------------------------------------------------------------

def get_recommendation(
    conn: sqlite3.Connection,
    job_title: str,
) -> tuple[str | None, float]:
    """Recommend a resume for a job based on past manual selections.

    Looks at all past selections where the job title shares words with
    this job title, and returns the most frequently chosen resume.

    Args:
        conn: Database connection.
        job_title: The job title to get a recommendation for.

    Returns:
        Tuple of (recommended_resume_id, confidence).
        confidence is 0.0-1.0 based on how many past selections agree.
        Returns (None, 0.0) if no data available.
    """
    if not job_title:
        return None, 0.0

    # Get all past selections
    rows = conn.execute(
        "SELECT job_title, chosen_resume_id FROM resume_selections"
    ).fetchall()

    if not rows:
        return None, 0.0

    title_words = set(job_title.lower().split())

    # Score each past selection by word overlap with current job title
    weighted_votes: Counter = Counter()
    total_weight = 0.0

    for row in rows:
        past_title = (row[0] or "").lower()
        past_resume = row[1]
        past_words = set(past_title.split())

        # Jaccard similarity: intersection / union
        if not past_words or not title_words:
            continue
        overlap = len(title_words & past_words)
        union = len(title_words | past_words)
        similarity = overlap / union if union > 0 else 0.0

        if similarity > 0.1:  # Minimum threshold
            weighted_votes[past_resume] += similarity
            total_weight += similarity

    if not weighted_votes or total_weight == 0:
        return None, 0.0

    # Most voted resume
    best_resume, best_weight = weighted_votes.most_common(1)[0]
    confidence = best_weight / total_weight if total_weight > 0 else 0.0

    return best_resume, round(confidence, 3)


# ---------------------------------------------------------------------------
# Learning System Settings
# ---------------------------------------------------------------------------

def get_learning_settings() -> dict:
    """Get learning system settings from manifest.

    Returns:
        Dict with keys:
        - auto_send_enabled: bool (default: False)
        - confidence_threshold: float 0.0-1.0 (default: 0.9)
    """
    manifest = load_resumes_manifest()
    learning = manifest.get("learning_settings", {})

    return {
        "auto_send_enabled": learning.get("auto_send_enabled", False),
        "confidence_threshold": learning.get("confidence_threshold", 0.9),
    }


def set_learning_settings(auto_send_enabled: bool = None, confidence_threshold: float = None) -> None:
    """Update learning system settings in manifest.

    Args:
        auto_send_enabled: Whether to auto-resolve conflicts if confidence exceeds threshold.
        confidence_threshold: Minimum confidence (0.0-1.0) to auto-send.
    """
    manifest = load_resumes_manifest()

    if "learning_settings" not in manifest:
        manifest["learning_settings"] = {}

    if auto_send_enabled is not None:
        manifest["learning_settings"]["auto_send_enabled"] = bool(auto_send_enabled)

    if confidence_threshold is not None:
        # Clamp to 0.0-1.0
        threshold = max(0.0, min(1.0, float(confidence_threshold)))
        manifest["learning_settings"]["confidence_threshold"] = round(threshold, 2)

    RESUMES_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("Updated learning settings: auto_send=%s, threshold=%.0f%%",
             manifest["learning_settings"].get("auto_send_enabled", False),
             manifest["learning_settings"].get("confidence_threshold", 0.9) * 100)


def should_auto_send_conflict(confidence: float) -> bool:
    """Check if a conflict should be auto-resolved based on settings.

    Args:
        confidence: Recommendation confidence (0.0-1.0).

    Returns:
        True if auto-send is enabled and confidence exceeds threshold.
    """
    settings = get_learning_settings()
    if not settings["auto_send_enabled"]:
        return False

    threshold = settings["confidence_threshold"]
    return confidence >= threshold
