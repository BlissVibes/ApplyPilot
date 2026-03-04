"""ApplyPilot Web GUI — local Flask-based interface for the full pipeline.

Launch with:  applypilot gui
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, render_template, send_file

from applypilot.config import (
    APP_DIR, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
    SEARCH_CONFIG_PATH, ENV_PATH, TAILORED_DIR, COVER_LETTER_DIR,
    RESUMES_DIR, RESUMES_MANIFEST_PATH,
    ensure_dirs, load_env, load_profile,
    get_resume_path, load_resumes_manifest, migrate_legacy_resume,
)
from applypilot.database import init_db, get_connection, get_stats, get_jobs_by_stage
from applypilot.resume_manager import (
    assign_resume_to_job, validate_resume_exists, get_default_resume_id, set_default_resume_id,
    set_job_title_keywords, get_job_title_keywords, get_matching_resumes,
    get_pending_conflicts, get_resolved_conflicts, resolve_conflict,
    get_recommendation,
)

log = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit

# ---------------------------------------------------------------------------
# Pipeline background runner
# ---------------------------------------------------------------------------

_pipeline_state: dict = {
    "running": False,
    "stages": [],
    "current_stage": None,
    "results": [],
    "error": None,
    "started_at": None,
    "finished_at": None,
}
_pipeline_lock = threading.Lock()


def _run_pipeline_bg(stages: list[str], min_score: int, workers: int,
                     validation_mode: str, stream: bool) -> None:
    """Run pipeline in a background thread, updating _pipeline_state."""
    global _pipeline_state

    with _pipeline_lock:
        _pipeline_state = {
            "running": True,
            "stages": stages,
            "current_stage": None,
            "results": [],
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
        }

    try:
        from applypilot.pipeline import (
            _STAGE_RUNNERS, _resolve_stages, STAGE_META,
        )

        ordered = _resolve_stages(stages)

        for name in ordered:
            with _pipeline_lock:
                _pipeline_state["current_stage"] = name

            t0 = time.time()
            runner = _STAGE_RUNNERS[name]
            kwargs: dict = {}
            if name in ("tailor", "cover"):
                kwargs["min_score"] = min_score
                kwargs["validation_mode"] = validation_mode
            if name in ("discover", "enrich"):
                kwargs["workers"] = workers

            try:
                result = runner(**kwargs)
                status = "ok"
                if isinstance(result, dict):
                    status = result.get("status", "ok")
            except Exception as e:
                status = f"error: {e}"
                log.exception("Stage '%s' failed", name)

            elapsed = time.time() - t0
            with _pipeline_lock:
                _pipeline_state["results"].append({
                    "stage": name,
                    "status": status,
                    "elapsed": round(elapsed, 1),
                })

    except Exception as e:
        with _pipeline_lock:
            _pipeline_state["error"] = str(e)
    finally:
        with _pipeline_lock:
            _pipeline_state["running"] = False
            _pipeline_state["current_stage"] = None
            _pipeline_state["finished_at"] = time.time()


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    # Migrate legacy resume on startup if needed
    migrate_legacy_resume()
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------

@app.route("/api/stats")
def api_stats():
    """Pipeline statistics."""
    try:
        stats = get_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/jobs")
def api_jobs():
    """List jobs with optional filters."""
    stage = request.args.get("stage", "discovered")
    min_score = request.args.get("min_score", type=int)
    limit = request.args.get("limit", 200, type=int)
    search = request.args.get("search", "").strip().lower()

    try:
        jobs = get_jobs_by_stage(stage=stage, min_score=min_score, limit=limit)
        if search:
            jobs = [
                j for j in jobs
                if search in (j.get("title") or "").lower()
                or search in (j.get("location") or "").lower()
                or search in (j.get("site") or "").lower()
            ]
        return jsonify(jobs)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/job/<path:url>")
def api_job_detail(url):
    """Single job details."""
    try:
        conn = get_connection()
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url,)).fetchone()
        if not row:
            return jsonify({"error": "Job not found"}), 404
        return jsonify(dict(zip(row.keys(), row)))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pipeline/run", methods=["POST"])
def api_pipeline_run():
    """Start pipeline stages in background."""
    with _pipeline_lock:
        if _pipeline_state["running"]:
            return jsonify({"error": "Pipeline is already running"}), 409

    data = request.get_json(force=True)
    stages = data.get("stages", ["all"])
    min_score = data.get("min_score", 7)
    workers = data.get("workers", 1)
    validation_mode = data.get("validation_mode", "normal")
    stream = data.get("stream", False)

    t = threading.Thread(
        target=_run_pipeline_bg,
        args=(stages, min_score, workers, validation_mode, stream),
        daemon=True,
    )
    t.start()

    return jsonify({"status": "started", "stages": stages})


@app.route("/api/pipeline/status")
def api_pipeline_status():
    """Get current pipeline run status."""
    with _pipeline_lock:
        return jsonify(dict(_pipeline_state))


@app.route("/api/profile", methods=["GET"])
def api_profile_get():
    """Get current profile."""
    try:
        if not PROFILE_PATH.exists():
            return jsonify({"exists": False})
        profile = load_profile()
        return jsonify({"exists": True, "profile": profile})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/profile", methods=["POST"])
def api_profile_save():
    """Save profile."""
    try:
        data = request.get_json(force=True)
        PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROFILE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/resume", methods=["GET"])
def api_resume_get():
    """Get base resume text."""
    if RESUME_PATH.exists():
        return jsonify({
            "exists": True,
            "text": RESUME_PATH.read_text(encoding="utf-8"),
            "filename": "resume.txt",
        })
    return jsonify({"exists": False})


@app.route("/api/resume/upload", methods=["POST"])
def api_resume_upload():
    """Upload resume file (.txt or .pdf)."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ensure_dirs()
    ext = Path(f.filename).suffix.lower()

    if ext == ".txt":
        content = f.read().decode("utf-8", errors="replace")
        RESUME_PATH.write_text(content, encoding="utf-8")
        return jsonify({"status": "uploaded", "filename": f.filename, "type": "txt"})

    elif ext == ".pdf":
        # Save PDF
        pdf_bytes = f.read()
        RESUME_PDF_PATH.write_bytes(pdf_bytes)

        # Extract text from PDF
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(pdf_bytes))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            RESUME_PATH.write_text(text, encoding="utf-8")
            return jsonify({
                "status": "uploaded",
                "filename": f.filename,
                "type": "pdf",
                "extracted_text": text[:500] + ("..." if len(text) > 500 else ""),
            })
        except Exception as e:
            return jsonify({"error": f"PDF uploaded but text extraction failed: {e}"}), 500

    else:
        return jsonify({"error": f"Unsupported file type: {ext}. Use .txt or .pdf"}), 400


# ─── Multiple Resume Management ──────────────────────────────────────────

@app.route("/api/resumes", methods=["GET"])
def api_resumes_list():
    """List all available resumes with metadata."""
    migrate_legacy_resume()
    manifest = load_resumes_manifest()
    return jsonify({
        "default_resume_id": manifest.get("default_resume_id", "default"),
        "resumes": manifest.get("resumes", []),
    })


@app.route("/api/resumes", methods=["POST"])
def api_resumes_upload():
    """Upload a new named resume."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    resume_name = request.form.get("resume_name", Path(f.filename).stem)
    resume_desc = request.form.get("resume_description", "")
    tags = request.form.get("resume_tags", "")

    ext = Path(f.filename).suffix.lower()
    if ext not in [".txt", ".pdf"]:
        return jsonify({"error": f"Unsupported file type: {ext}"}), 400

    ensure_dirs()

    # Create directory for resume
    resume_id = resume_name.lower().replace(" ", "-")
    resume_dir = RESUMES_DIR / resume_id
    resume_dir.mkdir(parents=True, exist_ok=True)

    # Extract text and save
    if ext == ".txt":
        content = f.read().decode("utf-8", errors="replace")
        (resume_dir / "resume.txt").write_text(content, encoding="utf-8")
        text_preview = content[:200]
    else:  # PDF
        pdf_bytes = f.read()
        (resume_dir / "resume.pdf").write_bytes(pdf_bytes)
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(pdf_bytes))
            content = "\n".join(page.extract_text() or "" for page in reader.pages)
            (resume_dir / "resume.txt").write_text(content, encoding="utf-8")
            text_preview = content[:200]
        except Exception as e:
            text_preview = f"[PDF uploaded, text extraction failed: {e}]"

    # Create metadata
    import json
    from datetime import datetime, timezone
    info = {
        "id": resume_id,
        "name": resume_name,
        "description": resume_desc,
        "path": str(resume_dir / "resume.txt"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tags": [t.strip() for t in tags.split(",") if t.strip()],
        "is_default": False,
    }
    (resume_dir / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")

    # Update manifest
    manifest = load_resumes_manifest()
    if "resumes" not in manifest:
        manifest["resumes"] = []
    # Remove if exists (update)
    manifest["resumes"] = [r for r in manifest["resumes"] if r.get("id") != resume_id]
    manifest["resumes"].append(info)
    RESUMES_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return jsonify({
        "status": "uploaded",
        "resume_id": resume_id,
        "name": resume_name,
        "text_preview": text_preview,
    })


@app.route("/api/resumes/<resume_id>", methods=["GET"])
def api_resume_detail(resume_id):
    """Get resume content and metadata."""
    if not validate_resume_exists(resume_id):
        return jsonify({"error": "Resume not found"}), 404

    resume_path = get_resume_path(resume_id)
    manifest = load_resumes_manifest()
    resume_info = next((r for r in manifest.get("resumes", []) if r.get("id") == resume_id), None)

    try:
        text = resume_path.read_text(encoding="utf-8")
    except Exception as e:
        return jsonify({"error": f"Failed to read resume: {e}"}), 500

    return jsonify({
        "id": resume_id,
        "info": resume_info,
        "text": text,
        "length": len(text),
    })


@app.route("/api/resumes/<resume_id>", methods=["DELETE"])
def api_resume_delete(resume_id):
    """Delete a resume (prevent deleting 'default')."""
    if resume_id == "default":
        return jsonify({"error": "Cannot delete the default resume"}), 400

    import shutil
    resume_dir = RESUMES_DIR / resume_id
    if resume_dir.exists():
        shutil.rmtree(resume_dir)

    # Update manifest
    manifest = load_resumes_manifest()
    manifest["resumes"] = [r for r in manifest.get("resumes", []) if r.get("id") != resume_id]
    RESUMES_MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return jsonify({"status": "deleted", "resume_id": resume_id})


@app.route("/api/resumes/<resume_id>/set-default", methods=["POST"])
def api_resume_set_default(resume_id):
    """Set default resume for auto-selection."""
    if not validate_resume_exists(resume_id):
        return jsonify({"error": "Resume not found"}), 404

    set_default_resume_id(resume_id)
    return jsonify({"status": "set", "default_resume_id": resume_id})


@app.route("/api/resumes/<resume_id>/keywords", methods=["GET"])
def api_resume_keywords_get(resume_id):
    """Get job title keywords for a resume."""
    if not validate_resume_exists(resume_id):
        return jsonify({"error": "Resume not found"}), 404

    keywords = get_job_title_keywords(resume_id)
    return jsonify({
        "resume_id": resume_id,
        "keywords": keywords,
    })


@app.route("/api/resumes/<resume_id>/keywords", methods=["POST"])
def api_resume_keywords_set(resume_id):
    """Set job title keywords for a resume.

    These keywords are used to auto-select this resume for matching job titles.
    """
    if not validate_resume_exists(resume_id):
        return jsonify({"error": "Resume not found"}), 404

    data = request.get_json() or {}
    keywords = data.get("keywords", [])

    # Normalize to list of strings
    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.replace(",", " ").split() if k.strip()]
    elif not isinstance(keywords, list):
        keywords = []

    set_job_title_keywords(resume_id, keywords)

    return jsonify({
        "status": "set",
        "resume_id": resume_id,
        "keywords": keywords,
    })


@app.route("/api/job/<path:url>/resume", methods=["GET"])
def api_job_resume_get(url):
    """Get currently selected resume for a job."""
    conn = get_connection()
    job = conn.execute("SELECT resume_id, resume_selection_method, selected_resume_path FROM jobs WHERE url=?", (url,)).fetchone()

    if not job:
        return jsonify({"error": "Job not found"}), 404

    return jsonify({
        "resume_id": job[0] or "default",
        "selection_method": job[1] or "auto",
        "selected_resume_path": job[2],
    })


@app.route("/api/job/<path:url>/resume", methods=["POST"])
def api_job_resume_set(url):
    """Override resume selection for a specific job."""
    data = request.get_json() or {}
    resume_id = data.get("resume_id", "default")

    if not validate_resume_exists(resume_id):
        return jsonify({"error": f"Resume '{resume_id}' not found"}), 404

    conn = get_connection()
    job = conn.execute("SELECT url FROM jobs WHERE url=?", (url,)).fetchone()
    if not job:
        return jsonify({"error": "Job not found"}), 404

    assign_resume_to_job(conn, url, resume_id, override=True)

    return jsonify({
        "status": "assigned",
        "url": url,
        "resume_id": resume_id,
        "selection_method": "override",
    })


@app.route("/api/job/<path:url>/resume-matches", methods=["GET"])
def api_job_resume_matches(url):
    """Get all resumes that match a job's title, sorted by specificity.

    Useful for detecting conflicts and showing which resumes would apply.
    """
    job = get_connection().execute(
        "SELECT title FROM jobs WHERE url=?", (url,)
    ).fetchone()

    if not job:
        return jsonify({"error": "Job not found"}), 404

    job_title = job[0] or ""
    matches = get_matching_resumes(job_title)

    return jsonify({
        "url": url,
        "job_title": job_title,
        "matches": matches,
        "match_count": len(matches),
        "primary_match": matches[0]["resume_id"] if matches else None,
    })


# ─── Resume Conflict Queue ────────────────────────────────────────────

@app.route("/api/resume-queue", methods=["GET"])
def api_resume_queue():
    """Get pending resume conflicts that need user review."""
    conn = get_connection()
    pending = get_pending_conflicts(conn)
    return jsonify({
        "pending": pending,
        "count": len(pending),
    })


@app.route("/api/resume-queue/history", methods=["GET"])
def api_resume_queue_history():
    """Get recently resolved conflicts."""
    limit = request.args.get("limit", 50, type=int)
    conn = get_connection()
    resolved = get_resolved_conflicts(conn, limit=limit)
    return jsonify({
        "resolved": resolved,
        "count": len(resolved),
    })


@app.route("/api/resume-queue/<path:url>/resolve", methods=["POST"])
def api_resume_queue_resolve(url):
    """User resolves a conflict by choosing a resume for the job."""
    data = request.get_json() or {}
    resume_id = data.get("resume_id")

    if not resume_id:
        return jsonify({"error": "resume_id is required"}), 400

    if not validate_resume_exists(resume_id):
        return jsonify({"error": f"Resume '{resume_id}' not found"}), 404

    conn = get_connection()

    # Verify the conflict exists
    row = conn.execute(
        "SELECT url FROM resume_conflict_queue WHERE url=? AND resolved_at IS NULL",
        (url,),
    ).fetchone()
    if not row:
        return jsonify({"error": "No pending conflict found for this job"}), 404

    resolve_conflict(conn, url, resume_id)

    return jsonify({
        "status": "resolved",
        "url": url,
        "chosen_resume_id": resume_id,
    })


@app.route("/api/resume-queue/<path:url>/skip", methods=["POST"])
def api_resume_queue_skip(url):
    """Skip a conflict by accepting the system's top match (specificity-based)."""
    conn = get_connection()

    row = conn.execute(
        "SELECT matches_json FROM resume_conflict_queue WHERE url=? AND resolved_at IS NULL",
        (url,),
    ).fetchone()
    if not row:
        return jsonify({"error": "No pending conflict found for this job"}), 404

    import json as _json
    matches = _json.loads(row[0]) if row[0] else []
    if not matches:
        return jsonify({"error": "No matches to auto-select from"}), 400

    # Use the top match (most specific)
    top_resume_id = matches[0]["resume_id"]
    resolve_conflict(conn, url, top_resume_id)

    return jsonify({
        "status": "resolved",
        "url": url,
        "chosen_resume_id": top_resume_id,
        "method": "auto_specificity",
    })


@app.route("/api/resume-queue/resolve-all", methods=["POST"])
def api_resume_queue_resolve_all():
    """Resolve all pending conflicts using recommendations or top specificity match."""
    data = request.get_json() or {}
    method = data.get("method", "recommendation")  # "recommendation" or "specificity"

    conn = get_connection()
    pending = get_pending_conflicts(conn)
    resolved_count = 0

    for conflict in pending:
        url = conflict["url"]
        matches = conflict["matches"]
        if not matches:
            continue

        if method == "recommendation" and conflict["recommendation_id"]:
            chosen = conflict["recommendation_id"]
        else:
            chosen = matches[0]["resume_id"]  # top specificity

        resolve_conflict(conn, url, chosen)
        resolved_count += 1

    return jsonify({
        "status": "resolved_all",
        "method": method,
        "resolved_count": resolved_count,
    })


@app.route("/api/resume-recommendation")
def api_resume_recommendation():
    """Get a resume recommendation for a given job title."""
    job_title = request.args.get("job_title", "")
    if not job_title:
        return jsonify({"error": "job_title parameter required"}), 400

    conn = get_connection()
    rec_id, confidence = get_recommendation(conn, job_title)

    return jsonify({
        "job_title": job_title,
        "recommendation_id": rec_id,
        "confidence": confidence,
    })


@app.route("/api/env", methods=["GET"])
def api_env_get():
    """Get .env configuration (masked keys)."""
    env_data = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("'\"")
                # Mask API keys
                if "KEY" in key.upper() and value:
                    masked = value[:4] + "..." + value[-4:] if len(value) > 8 else "****"
                    env_data[key] = {"value": masked, "is_set": True}
                else:
                    env_data[key] = {"value": value, "is_set": bool(value)}
    return jsonify(env_data)


@app.route("/api/env", methods=["POST"])
def api_env_save():
    """Save .env configuration."""
    try:
        data = request.get_json(force=True)
        lines = []
        for key, value in data.items():
            if value:  # Only write non-empty values
                lines.append(f"{key}={value}")
        ensure_dirs()
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # Reload env
        load_env()
        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/searches", methods=["GET"])
def api_searches_get():
    """Get search configuration as raw YAML or structured JSON."""
    try:
        fmt = request.args.get("format", "raw")
        if not SEARCH_CONFIG_PATH.exists():
            if fmt == "json":
                return jsonify({"exists": False, "data": {
                    "queries": [], "locations": [], "location": {"accept_patterns": [], "reject_patterns": []},
                    "country": "USA", "boards": ["indeed", "linkedin", "glassdoor", "zip_recruiter", "google"],
                    "defaults": {"results_per_site": 100, "hours_old": 72, "days_old": 3},
                    "exclusions": {"titles": [], "experience": [], "description": [], "salary": []},
                    "global_remote_locations": [],
                }})
            return jsonify({"exists": False})

        content = SEARCH_CONFIG_PATH.read_text(encoding="utf-8")
        if fmt == "json":
            import yaml
            data = yaml.safe_load(content) or {}
            # Normalise for the form
            data.setdefault("queries", [])
            data.setdefault("locations", [])
            data.setdefault("location", {})
            data["location"].setdefault("accept_patterns", [])
            data["location"].setdefault("reject_patterns", [])
            data.setdefault("country", "USA")
            data.setdefault("boards", ["indeed", "linkedin", "glassdoor", "zip_recruiter", "google"])

            # Handle defaults: convert hours_old to days_old if needed
            defaults = data.setdefault("defaults", {})
            if "hours_old" in defaults and "days_old" not in defaults:
                defaults["days_old"] = max(1, defaults["hours_old"] // 24)
            defaults.setdefault("results_per_site", 100)
            defaults.setdefault("hours_old", defaults.get("days_old", 3) * 24)

            # Handle old exclude_titles field -> new exclusions format
            if "exclude_titles" in data and "exclusions" not in data:
                data["exclusions"] = {
                    "titles": data.pop("exclude_titles", []),
                    "experience": [],
                    "description": [],
                    "salary": [],
                }
            else:
                data.setdefault("exclusions", {"titles": [], "experience": [], "description": [], "salary": []})

            # Add global_remote_locations if not present
            data.setdefault("global_remote_locations", [])

            return jsonify({"exists": True, "data": data})
        return jsonify({"exists": True, "content": content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/searches", methods=["POST"])
def api_searches_save():
    """Save search configuration from raw YAML or structured JSON."""
    try:
        data = request.get_json(force=True)
        ensure_dirs()

        if "content" in data:
            # Raw YAML mode
            SEARCH_CONFIG_PATH.write_text(data["content"], encoding="utf-8")
        elif "data" in data:
            # Structured JSON mode — convert to YAML
            import yaml
            SEARCH_CONFIG_PATH.write_text(
                yaml.dump(data["data"], default_flow_style=False, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
        else:
            return jsonify({"error": "Provide 'content' (YAML) or 'data' (JSON)"}), 400

        return jsonify({"status": "saved"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/doctor")
def api_doctor():
    """Run doctor checks."""
    import shutil
    load_env()

    checks = []

    # Profile
    checks.append({
        "name": "profile.json",
        "ok": PROFILE_PATH.exists(),
        "note": str(PROFILE_PATH) if PROFILE_PATH.exists() else "Run setup to create",
    })

    # Resume
    checks.append({
        "name": "resume.txt",
        "ok": RESUME_PATH.exists(),
        "note": str(RESUME_PATH) if RESUME_PATH.exists() else "Upload your resume",
    })

    # Search config
    checks.append({
        "name": "searches.yaml",
        "ok": SEARCH_CONFIG_PATH.exists(),
        "note": str(SEARCH_CONFIG_PATH) if SEARCH_CONFIG_PATH.exists() else "Will use example config",
    })

    # LLM
    try:
        from applypilot.llm import resolve_llm_config
        cfg = resolve_llm_config()
        checks.append({
            "name": "LLM API Key",
            "ok": True,
            "note": f"{cfg.provider} ({cfg.model})",
        })
    except Exception:
        checks.append({
            "name": "LLM API Key",
            "ok": False,
            "note": "Set GEMINI_API_KEY, OPENAI_API_KEY, or ANTHROPIC_API_KEY",
        })

    # Claude CLI
    claude_bin = shutil.which("claude")
    checks.append({
        "name": "Claude Code CLI",
        "ok": bool(claude_bin),
        "note": claude_bin or "Needed for auto-apply",
    })

    # Chrome
    try:
        from applypilot.config import get_chrome_path
        chrome = get_chrome_path()
        checks.append({"name": "Chrome", "ok": True, "note": chrome})
    except FileNotFoundError:
        checks.append({"name": "Chrome", "ok": False, "note": "Needed for auto-apply"})

    # Tier
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()

    return jsonify({"checks": checks, "tier": tier, "tier_label": TIER_LABELS.get(tier, "")})


@app.route("/api/tailored")
def api_tailored_list():
    """List generated tailored resumes and cover letters."""
    resumes = []
    covers = []

    if TAILORED_DIR.exists():
        for f in sorted(TAILORED_DIR.iterdir()):
            if f.suffix in (".txt", ".pdf"):
                resumes.append({
                    "filename": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                    "type": f.suffix[1:],
                })

    if COVER_LETTER_DIR.exists():
        for f in sorted(COVER_LETTER_DIR.iterdir()):
            if f.suffix in (".txt", ".pdf"):
                covers.append({
                    "filename": f.name,
                    "path": str(f),
                    "size": f.stat().st_size,
                    "type": f.suffix[1:],
                })

    return jsonify({"resumes": resumes, "covers": covers})


@app.route("/api/file/view")
def api_file_view():
    """View a generated file's content."""
    filepath = request.args.get("path", "")
    if not filepath:
        return jsonify({"error": "No path provided"}), 400

    # Security: only allow files within APP_DIR
    resolved = Path(filepath).resolve()
    if not str(resolved).startswith(str(APP_DIR.resolve())):
        return jsonify({"error": "Access denied"}), 403

    if not resolved.exists():
        return jsonify({"error": "File not found"}), 404

    if resolved.suffix == ".pdf":
        return send_file(str(resolved), mimetype="application/pdf")

    content = resolved.read_text(encoding="utf-8", errors="replace")
    return jsonify({"content": content, "filename": resolved.name})


@app.route("/api/file/download")
def api_file_download():
    """Download a generated file."""
    filepath = request.args.get("path", "")
    if not filepath:
        return jsonify({"error": "No path provided"}), 400

    resolved = Path(filepath).resolve()
    if not str(resolved).startswith(str(APP_DIR.resolve())):
        return jsonify({"error": "Access denied"}), 403

    if not resolved.exists():
        return jsonify({"error": "File not found"}), 404

    return send_file(str(resolved), as_attachment=True)


@app.route("/api/parse-resume", methods=["POST"])
def api_parse_resume():
    """Parse uploaded resume to extract profile data using LLM."""
    try:
        if not RESUME_PATH.exists():
            return jsonify({"error": "No resume uploaded yet"}), 400

        resume_text = RESUME_PATH.read_text(encoding="utf-8")

        from applypilot.wizard.resume_parser import extract_resume_data, extracted_to_profile
        extracted, meta = extract_resume_data(resume_text)

        if not meta.get("success"):
            return jsonify({
                "error": "Resume parsing failed",
                "details": meta.get("errors", []),
            }), 500

        profile = extracted_to_profile(extracted)
        return jsonify({"status": "parsed", "profile": profile, "meta": meta})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def start_gui(host: str = "127.0.0.1", port: int = 5000, debug: bool = False) -> None:
    """Start the web GUI server."""
    load_env()
    ensure_dirs()
    init_db()

    log.info("Starting ApplyPilot GUI at http://%s:%d", host, port)
    print(f"\n  ApplyPilot GUI running at http://{host}:{port}\n")

    app.run(host=host, port=port, debug=debug, use_reloader=False)
