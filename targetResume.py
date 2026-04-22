import io
import os
import json


from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from openai import OpenAI
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from dotenv import load_dotenv

load_dotenv(override=True)


def get_required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set.")
    return value


def normalize_text_block(value):
    if value is None:
        return ""
    return str(value).strip()


def parse_bullets(raw_value):
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        values = raw_value
    else:
        values = str(raw_value).splitlines()

    bullets = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned:
            continue
        if cleaned.startswith(("- ", "* ")):
            cleaned = cleaned[2:].strip()
        bullets.append(cleaned)
    return bullets


def normalize_resume_entries(raw_value):
    if not raw_value:
        return []

    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except json.JSONDecodeError:
            return []

    if not isinstance(raw_value, list):
        return []

    entries = []
    for item in raw_value:
        if not isinstance(item, dict):
            continue

        title = normalize_text_block(item.get("title"))
        details = normalize_text_block(item.get("details"))
        bullets = parse_bullets(item.get("bullets"))

        if title or details or bullets:
            entries.append({
                "title": title,
                "details": details,
                "bullets": bullets
            })

    return entries


def format_resume_entries(entries):
    sections = []
    for entry in entries:
        title = normalize_text_block(entry.get("title"))
        details = normalize_text_block(entry.get("details"))
        bullets = parse_bullets(entry.get("bullets"))

        header_parts = [part for part in [title, details] if part]
        lines = []
        if header_parts:
            lines.append(" | ".join(header_parts))
        lines.extend(f"- {bullet}" for bullet in bullets)

        if lines:
            sections.append("\n".join(lines))

    return "\n\n".join(sections)


def normalize_skills_entries(raw_value):
    if not raw_value:
        return []

    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except json.JSONDecodeError:
            return []

    if not isinstance(raw_value, list):
        return []

    entries = []
    for item in raw_value:
        if not isinstance(item, dict):
            continue

        category = normalize_text_block(item.get("category"))
        values = item.get("values", [])
        if isinstance(values, str):
            values = [part.strip() for part in values.split(",")]

        normalized_values = [normalize_text_block(value) for value in values if normalize_text_block(value)]
        if category or normalized_values:
            entries.append({
                "category": category,
                "values": normalized_values
            })

    return entries


def format_skills_entries(entries):
    lines = []
    for entry in entries:
        category = normalize_text_block(entry.get("category"))
        values = [normalize_text_block(value) for value in entry.get("values", []) if normalize_text_block(value)]
        if not category and not values:
            continue
        if category and values:
            lines.append(f"- {category}: {', '.join(values)}")
        elif category:
            lines.append(f"- {category}")
        else:
            lines.append(f"- {', '.join(values)}")
    return "\n".join(lines)


def prepare_profile_for_view(profile):
    profile = profile or {}
    profile["skills_entries"] = normalize_skills_entries(profile.get("skills_entries"))
    profile["projects_entries"] = normalize_resume_entries(profile.get("projects_entries"))
    profile["experience_entries"] = normalize_resume_entries(profile.get("experience_entries"))
    return profile


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "targetresume_dev_secret_key")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

MONGODB_URI = get_required_env("MONGODB_URI")

client = MongoClient(MONGODB_URI)

db = client["TargetResume"]
users_collection = db["users"]
resumes_collection = db["resumes"]
jobs_collection = db["job_tracker"]
profiles_collection = db["profiles"]

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "5025"))


@app.route("/")
def home():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    profile = prepare_profile_for_view(profiles_collection.find_one({"user_id": user_id}))

    return render_template("dashboard.html", profile=profile)


@app.route("/generate-resume-preview", methods=["POST"])
def generate_resume_preview():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user_id = session["user_id"]
    profile = profiles_collection.find_one({"user_id": user_id}) or {}
    skills_entries = normalize_skills_entries(profile.get("skills_entries"))
    project_entries = normalize_resume_entries(profile.get("projects_entries"))
    experience_entries = normalize_resume_entries(profile.get("experience_entries"))

    education_top = ""
    if profile.get("school"):
        education_top += profile.get("school", "")
    if profile.get("school_location"):
        education_top += f", {profile.get('school_location')}"
    if profile.get("expected_grad"):
        education_top += f" Expected Graduation {profile.get('expected_grad')}"

    response = {
        "name": profile.get("name", "Your Name"),
        "contact": " | ".join(filter(None, [
            profile.get("email"),
            profile.get("phone"),
            profile.get("linkedin"),
            profile.get("portfolio")
        ])) or "Email | Phone | LinkedIn | Portfolio",
        "education_top": education_top,
        "education_bottom": profile.get("degree", ""),
        "skills": format_skills_entries(skills_entries) or profile.get("skills", ""),
        "projects": format_resume_entries(project_entries) or profile.get("projects", ""),
        "experience": format_resume_entries(experience_entries) or profile.get("experience", ""),
        "skills_entries": skills_entries,
        "projects_entries": project_entries,
        "experience_entries": experience_entries
    }

    return jsonify(response)

@app.route("/ai-rewrite-preview", methods=["POST"])
def ai_rewrite_preview():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    if not openai_client:
        return jsonify({"error": "OPENAI_API_KEY is not set."}), 500

    user_id = session["user_id"]
    profile = profiles_collection.find_one({"user_id": user_id}) or {}

    job_title = request.form.get("job_title", "").strip()
    job_description = request.form.get("job_description", "").strip()
    notes = request.form.get("notes", "").strip()

    if not job_description:
        return jsonify({"error": "Job description is required."}), 400

    education_text = ""
    if profile.get("school"):
        education_text += profile.get("school", "")
    if profile.get("school_location"):
        education_text += f", {profile.get('school_location')}"
    if profile.get("expected_grad"):
        education_text += f" Expected Graduation {profile.get('expected_grad')}"
    if profile.get("degree"):
        education_text += f"\n{profile.get('degree')}"

    prompt = f"""
You are helping tailor a resume for a specific job.

Use ONLY the user's provided information.
Do NOT invent employers, projects, dates, degrees, metrics, or skills.
Do NOT create a professional summary.
Rewrite only these sections:
skills
projects
experience

IMPORTANT:
Return valid JSON.
Each value must be a SINGLE STRING.
Do NOT return arrays.
Do NOT return nested objects.
Do NOT return bullet objects.
Use plain text only.

Return exactly this JSON format:
{{
  "skills": "plain text string",
  "projects": "plain text string",
  "experience": "plain text string"
}}

Target job title:
{job_title}

Target job description:
{job_description}

Special focus from user:
{notes}

User profile data:
Name: {profile.get("name", "")}
Education: {education_text}
Skills: {profile.get("skills", "")}
Projects: {profile.get("projects", "")}
Experience: {profile.get("experience", "")}
Certifications: {profile.get("certifications", "")}
"""

    try:
        response = openai_client.responses.create(
            model="gpt-5.4-mini",
            input=prompt
        )

        raw_text = response.output_text.strip()
        parsed = json.loads(raw_text)

        def normalize_to_text(value):
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                return "\n".join(str(item) for item in value)
            if isinstance(value, dict):
                return "\n".join(f"{k}: {v}" for k, v in value.items())
            return str(value)

        return jsonify({
            "skills": normalize_to_text(parsed.get("skills", profile.get("skills", ""))),
            "projects": normalize_to_text(parsed.get("projects", profile.get("projects", ""))),
            "experience": normalize_to_text(parsed.get("experience", profile.get("experience", "")))
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/save-resume-version", methods=["POST"])
def save_resume_version():
    if "user_id" not in session:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    user_id = session["user_id"]
    profile = profiles_collection.find_one({"user_id": user_id}) or {}

    job_title = request.form.get("job_title", "").strip()
    folder = request.form.get("folder", "").strip() or "Saved Drafts"

    resume_doc = {
        "user_id": user_id,
        "title": job_title if job_title else "Untitled Resume",
        "folder": folder,
        "job_title": job_title,
        "job_description": request.form.get("job_description"),
        "notes": request.form.get("notes"),

        "name": profile.get("name", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "linkedin": profile.get("linkedin", ""),
        "portfolio": profile.get("portfolio", ""),

        "school": profile.get("school", ""),
        "school_location": profile.get("school_location", ""),
        "expected_grad": profile.get("expected_grad", ""),
        "degree": profile.get("degree", ""),

        "skills": request.form.get("tailored_skills"),
        "projects": request.form.get("tailored_projects"),
        "experience": request.form.get("tailored_experience"),

        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    result = resumes_collection.insert_one(resume_doc)

    return jsonify({
        "success": True,
        "resume_id": str(result.inserted_id)
    })


@app.route("/export-resume")
def export_resume():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    profile = profiles_collection.find_one({"user_id": user_id}) or {}
    skills_entries = normalize_skills_entries(profile.get("skills_entries"))
    project_entries = normalize_resume_entries(profile.get("projects_entries"))
    experience_entries = normalize_resume_entries(profile.get("experience_entries"))

    tailored_skills = request.args.get(
        "tailored_skills",
        format_skills_entries(skills_entries) or profile.get("skills", "")
    )
    tailored_projects = request.args.get(
        "tailored_projects",
        format_resume_entries(project_entries) or profile.get("projects", "")
    )
    tailored_experience = request.args.get(
        "tailored_experience",
        format_resume_entries(experience_entries) or profile.get("experience", "")
    )

    education_top = ""
    if profile.get("school"):
        education_top += profile.get("school", "")
    if profile.get("school_location"):
        education_top += f", {profile.get('school_location')}"
    if profile.get("expected_grad"):
        education_top += f" Expected Graduation {profile.get('expected_grad')}"

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)

    y = 760
    p.setFont("Helvetica-Bold", 16)
    p.drawString(72, y, profile.get("name", "Your Name"))
    y -= 20

    p.setFont("Helvetica", 10)
    contact = " | ".join(filter(None, [
        profile.get("email"),
        profile.get("phone"),
        profile.get("linkedin"),
        profile.get("portfolio")
    ]))
    p.drawString(72, y, contact)
    y -= 30

    sections = [
        ("EDUCATION", education_top + ("\n" + profile.get("degree", "") if profile.get("degree") else "")),
        ("TECHNICAL SKILLS", tailored_skills),
        ("PROJECTS", tailored_projects),
        ("WORK EXPERIENCE", tailored_experience)
    ]

    for title, content in sections:
        p.setFont("Helvetica-Bold", 12)
        p.drawString(72, y, title)
        y -= 18

        p.setFont("Helvetica", 10)
        for line in content.split("\n"):
            if y < 72:
                p.showPage()
                y = 760
            p.drawString(72, y, line[:100])
            y -= 14

        y -= 10

    p.save()
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="target_resume_export.pdf",
        mimetype="application/pdf"
    )


@app.route("/resumes")
def resumes():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    selected_folder = request.args.get("folder", "All Resumes")

    all_resumes = list(
        resumes_collection.find({"user_id": user_id}).sort("updated_at", -1)
    )

    folder_names = sorted({
        resume.get("folder", "Saved Drafts")
        for resume in all_resumes
        if resume.get("folder")
    })

    if selected_folder == "All Resumes":
        filtered_resumes = all_resumes
    else:
        filtered_resumes = [
            resume for resume in all_resumes
            if resume.get("folder", "Saved Drafts") == selected_folder
        ]

    return render_template(
        "resumes.html",
        resumes=filtered_resumes,
        folder_names=folder_names,
        selected_folder=selected_folder
    )

@app.route("/delete-resume/<resume_id>", methods=["POST"])
def delete_resume(resume_id):
    if "user_id" not in session:
        return jsonify({"success": False}), 401

    user_id = session["user_id"]

    result = resumes_collection.delete_one({
        "_id": ObjectId(resume_id),
        "user_id": user_id
    })

    if result.deleted_count == 1:
        return jsonify({"success": True})

    return jsonify({"success": False}), 404

@app.route("/profile", methods=["GET"])
def profile():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    profile_data = prepare_profile_for_view(profiles_collection.find_one({"user_id": user_id}))

    return render_template("profile.html", profile=profile_data)


@app.route("/save-profile", methods=["POST"])
def save_profile():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    skills_entries = normalize_skills_entries(request.form.get("skills_entries"))
    projects_entries = normalize_resume_entries(request.form.get("projects_entries"))
    experience_entries = normalize_resume_entries(request.form.get("experience_entries"))

    profile_doc = {
        "user_id": user_id,
        "name": request.form.get("fullname"),
        "email": request.form.get("email"),
        "phone": request.form.get("phone"),
        "location": request.form.get("location"),
        "linkedin": request.form.get("linkedin"),
        "github": request.form.get("github"),
        "portfolio": request.form.get("portfolio"),

        "school": request.form.get("school"),
        "school_location": request.form.get("school_location"),
        "expected_grad": request.form.get("expected_grad"),
        "degree": request.form.get("degree"),

        "skills": format_skills_entries(skills_entries),
        "skills_entries": skills_entries,
        "projects": format_resume_entries(projects_entries),
        "experience": format_resume_entries(experience_entries),
        "projects_entries": projects_entries,
        "experience_entries": experience_entries,
        "certifications": request.form.get("certifications"),
        "updated_at": datetime.utcnow()
    }

    profiles_collection.update_one(
        {"user_id": user_id},
        {"$set": profile_doc},
        upsert=True
    )

    return redirect(url_for("profile"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        password = request.form.get("password")

        existing_user = users_collection.find_one({"email": email})
        if existing_user:
            return "An account with that email already exists."

        hashed_password = generate_password_hash(password)

        new_user = {
            "name": name,
            "email": email,
            "password": hashed_password,
            "created_at": datetime.utcnow()
        }

        result = users_collection.insert_one(new_user)

        session["user_id"] = str(result.inserted_id)
        session["user_name"] = name

        return redirect(url_for("dashboard"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email")
        password = request.form.get("password")

        user = users_collection.find_one({"email": email})

        if user and check_password_hash(user["password"], password):
            session["user_id"] = str(user["_id"])
            session["user_name"] = user["name"]
            return redirect(url_for("dashboard"))

        return "Invalid email or password."

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/job-tracker")
def job_tracker():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    jobs = list(jobs_collection.find({"user_id": user_id}))

    grouped_jobs = {
        "Saved": [],
        "Applied": [],
        "Interview": [],
        "Offer": [],
        "Rejected": []
    }

    for job in jobs:
        status = job.get("status", "Saved")
        if status in grouped_jobs:
            grouped_jobs[status].append(job)

    return render_template("job_tracker.html", grouped_jobs=grouped_jobs)


@app.route("/add-job", methods=["POST"])
def add_job():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]

    new_job = {
        "user_id": user_id,
        "company": request.form.get("company"),
        "job_title": request.form.get("job_title"),
        "location": request.form.get("location"),
        "status": request.form.get("status"),
        "resume_name": request.form.get("resume_name"),
        "job_link": request.form.get("job_link"),
        "notes": request.form.get("notes"),
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow()
    }

    jobs_collection.insert_one(new_job)
    return redirect(url_for("job_tracker"))


@app.route("/delete-job/<job_id>", methods=["POST"])
def delete_job(job_id):
    if "user_id" not in session:
        return jsonify({"success": False}), 401

    user_id = session["user_id"]

    result = jobs_collection.delete_one({
        "_id": ObjectId(job_id),
        "user_id": user_id
    })

    if result.deleted_count == 1:
        return jsonify({"success": True})

    return jsonify({"success": False}), 404

if __name__ == "__main__":
    app.run(host=HOST, port=PORT)
