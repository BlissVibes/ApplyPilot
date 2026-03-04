"""Resume management: load, select, validate per-job resume selection."""

import sqlite3
from pathlib import Path

from applypilot.config import get_resume_path, load_resumes_manifest


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
