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
from reportlab.pdfbase.pdfmetrics import stringWidth
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


def parse_skills_text_to_entries(text):
    entries = []
    for raw_line in normalize_text_block(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        parts = line.split(":")
        if len(parts) > 1:
            category = parts[0].strip()
            values = [item.strip() for item in ":".join(parts[1:]).split(",") if item.strip()]
        else:
            category = ""
            values = [item.strip() for item in line.split(",") if item.strip()]
        if category or values:
            entries.append({
                "category": category,
                "values": values
            })
    return entries


def parse_resume_text_to_entries(text):
    entries = []
    current = None
    for raw_line in normalize_text_block(text).splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                entries.append(current)
                current = None
            continue
        if line.startswith("- "):
            if current is None:
                current = {"title": "", "details": "", "bullets": []}
            current["bullets"].append(line[2:].strip())
        else:
            if current:
                entries.append(current)
            parts = [part.strip() for part in line.split("|")]
            current = {
                "title": parts[0] if parts else "",
                "details": " | ".join(parts[1:]) if len(parts) > 1 else "",
                "bullets": []
            }
    if current:
        entries.append(current)
    return [entry for entry in entries if entry.get("title") or entry.get("details") or entry.get("bullets")]


def prepare_profile_for_view(profile):
    profile = profile or {}
    profile["skills_entries"] = normalize_skills_entries(profile.get("skills_entries")) or parse_skills_text_to_entries(profile.get("skills", ""))
    profile["projects_entries"] = normalize_resume_entries(profile.get("projects_entries")) or parse_resume_text_to_entries(profile.get("projects", ""))
    profile["experience_entries"] = normalize_resume_entries(profile.get("experience_entries")) or parse_resume_text_to_entries(profile.get("experience", ""))
    return profile


def prepare_resume_for_view(resume):
    resume = resume or {}
    resume["skills_entries"] = normalize_skills_entries(resume.get("skills_entries")) or parse_skills_text_to_entries(resume.get("skills", ""))
    resume["projects_entries"] = normalize_resume_entries(resume.get("projects_entries")) or parse_resume_text_to_entries(resume.get("projects", ""))
    resume["experience_entries"] = normalize_resume_entries(resume.get("experience_entries")) or parse_resume_text_to_entries(resume.get("experience", ""))
    return resume


def parse_ai_rewrite_response(parsed, profile):
    skills_entries = normalize_skills_entries(parsed.get("skills_entries"))
    project_entries = normalize_resume_entries(parsed.get("projects_entries"))
    experience_entries = normalize_resume_entries(parsed.get("experience_entries"))
    fallback_projects = normalize_resume_entries(profile.get("projects_entries"))

    if not skills_entries:
        skills_entries = normalize_skills_entries(profile.get("skills_entries"))
    if not project_entries:
        project_entries = fallback_projects
    if not experience_entries:
        experience_entries = normalize_resume_entries(profile.get("experience_entries"))

    if len(project_entries) < 3 and fallback_projects:
        seen = {
            (
                normalize_text_block(entry.get("title")),
                normalize_text_block(entry.get("details"))
            )
            for entry in project_entries
        }
        for fallback_entry in fallback_projects:
            key = (
                normalize_text_block(fallback_entry.get("title")),
                normalize_text_block(fallback_entry.get("details"))
            )
            if key in seen:
                continue
            project_entries.append(fallback_entry)
            seen.add(key)
            if len(project_entries) >= 3:
                break

    return {
        "skills": format_skills_entries(skills_entries) or profile.get("skills", ""),
        "projects": format_resume_entries(project_entries) or profile.get("projects", ""),
        "experience": format_resume_entries(experience_entries) or profile.get("experience", ""),
        "skills_entries": skills_entries,
        "projects_entries": project_entries,
        "experience_entries": experience_entries
    }


def split_text_to_lines(text, font_name, font_size, max_width):
    text = normalize_text_block(text)
    if not text:
        return []

    lines = []
    for paragraph in text.splitlines():
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue

        words = paragraph.split()
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if stringWidth(candidate, font_name, font_size) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)

    return lines


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
folders_collection = db["folders"]

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
    resume_id = request.args.get("resume_id", "").strip()
    selected_resume = None

    if resume_id:
        selected_resume = resumes_collection.find_one({
            "_id": ObjectId(resume_id),
            "user_id": user_id
        })
        selected_resume = prepare_resume_for_view(selected_resume)

    return render_template("dashboard.html", profile=profile, selected_resume=selected_resume)


@app.route("/generate-resume-preview", methods=["POST"])
def generate_resume_preview():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user_id = session["user_id"]
    profile = profiles_collection.find_one({"user_id": user_id}) or {}
    skills_entries = normalize_skills_entries(profile.get("skills_entries"))
    project_entries = normalize_resume_entries(profile.get("projects_entries"))
    experience_entries = normalize_resume_entries(profile.get("experience_entries"))

    education_school = ""
    if profile.get("school"):
        education_school += profile.get("school", "")
    if profile.get("school_location"):
        education_school += f", {profile.get('school_location')}"

    response = {
        "name": profile.get("name", "Your Name"),
        "contact": " | ".join(filter(None, [
            profile.get("email"),
            profile.get("phone"),
            profile.get("linkedin"),
            profile.get("portfolio")
        ])) or "Email | Phone | LinkedIn | Portfolio",
        "education_top": education_school,
        "education_grad": profile.get("expected_grad", ""),
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
    profile = prepare_profile_for_view(profiles_collection.find_one({"user_id": user_id}))

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
        education_text += f" Graduation {profile.get('expected_grad')}"
    if profile.get("degree"):
        education_text += f"\n{profile.get('degree')}"

    source_skills = profile.get("skills_entries") or profile.get("skills", "")
    source_projects = profile.get("projects_entries") or profile.get("projects", "")
    source_experience = profile.get("experience_entries") or profile.get("experience", "")

    prompt = f"""
You are helping tailor a resume for a specific job.

Use ONLY the user's provided information.
Do NOT invent employers, projects, dates, degrees, metrics, or skills.
Do NOT create a professional summary.
Rewrite only these sections:
skills
projects
experience

The output must fit a strong one-page student resume.
Select only the strongest and most relevant details for this specific target job.
Do not include everything if that would make the resume too long.
Prefer concise, high-impact bullets.
Return at least 3 projects when the user has 3 or more available.
Order projects from strongest and most relevant first to weakest and least relevant last.
Keep 2-4 bullets per project or experience entry.
If an entry has 4 strong relevant bullets, keep all 4 instead of shortening it unnecessarily.

IMPORTANT:
Return valid JSON.

Return exactly this JSON format:
{{
  "skills_entries": [
    {{
      "category": "Languages",
      "values": ["Java", "Python", "JavaScript"]
    }}
  ],
  "projects_entries": [
    {{
      "title": "Project name",
      "details": "optional short detail line such as tech stack",
      "bullets": [
        "short bullet",
        "short bullet"
      ]
    }}
  ],
  "experience_entries": [
    {{
      "title": "Role or organization",
      "details": "optional short detail line such as company | location | dates",
      "bullets": [
        "short bullet",
        "short bullet"
      ]
    }}
  ]
}}

Do not return any keys other than:
skills_entries
projects_entries
experience_entries

Target job title:
{job_title}

Target job description:
{job_description}

Special focus from user:
{notes}

User profile data:
Name: {profile.get("name", "")}
Education: {education_text}
Skills: {json.dumps(source_skills, ensure_ascii=True)}
Projects: {json.dumps(source_projects, ensure_ascii=True)}
Experience: {json.dumps(source_experience, ensure_ascii=True)}
Certifications: {profile.get("certifications", "")}
"""

    try:
        response = openai_client.responses.create(
            model="gpt-5.4-mini",
            input=prompt
        )

        raw_text = response.output_text.strip()
        parsed = json.loads(raw_text)
        return jsonify(parse_ai_rewrite_response(parsed, profile))

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
        "skills_entries": normalize_skills_entries(request.form.get("skills_entries")),
        "projects_entries": normalize_resume_entries(request.form.get("projects_entries")),
        "experience_entries": normalize_resume_entries(request.form.get("experience_entries")),

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

    buffer = io.BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    page_width, page_height = letter
    left_margin = 48
    right_margin = 48
    content_width = page_width - left_margin - right_margin
    y = page_height - 42
    bottom_margin = 52

    education_school = ""
    if profile.get("school"):
        education_school += profile.get("school", "")
    if profile.get("school_location"):
        education_school += f", {profile.get('school_location')}"
    education_grad = profile.get("expected_grad", "")
    education_degree = profile.get("degree", "")

    skills_lines = split_text_to_lines(tailored_skills.replace("\u2022", "-"), "Times-Roman", 10.5, content_width - 14)

    def new_page():
        nonlocal y
        p.showPage()
        y = page_height - 42

    def ensure_space(required_height):
        nonlocal y
        if y - required_height < bottom_margin:
            new_page()

    def draw_section_title(title):
        nonlocal y
        ensure_space(30)
        y -= 4
        p.setFont("Times-Bold", 11)
        p.drawString(left_margin, y, title)
        y -= 6
        p.setLineWidth(0.8)
        p.line(left_margin, y, page_width - right_margin, y)
        y -= 14

    def draw_wrapped_line(text, font_name="Times-Roman", font_size=10.5, indent=0, leading=13):
        nonlocal y
        max_width = content_width - indent
        lines = split_text_to_lines(text, font_name, font_size, max_width)
        for line in lines:
            ensure_space(leading)
            p.setFont(font_name, font_size)
            p.drawString(left_margin + indent, y, line)
            y -= leading

    def draw_bullet_list(lines):
        nonlocal y
        bullet_indent = 10
        text_indent = 20
        for bullet in lines:
            clean_bullet = bullet.strip()
            if not clean_bullet:
                continue
            if clean_bullet.startswith("- "):
                clean_bullet = clean_bullet[2:].strip()

            wrapped = split_text_to_lines(clean_bullet, "Times-Roman", 10.5, content_width - text_indent)
            if not wrapped:
                continue

            ensure_space(13 * len(wrapped))
            p.setFont("Times-Roman", 10.5)
            p.drawString(left_margin + bullet_indent, y, u"\u2022")
            p.drawString(left_margin + text_indent, y, wrapped[0])
            y -= 13
            for continuation in wrapped[1:]:
                ensure_space(13)
                p.drawString(left_margin + text_indent, y, continuation)
                y -= 13

    def draw_two_column_skills(lines):
        nonlocal y
        cleaned_lines = []
        for line in lines:
            clean = line.strip()
            if not clean:
                continue
            if clean.startswith("- "):
                clean = clean[2:].strip()
            cleaned_lines.append(clean)

        if not cleaned_lines:
            return

        column_gap = 22
        column_width = (content_width - column_gap) / 2
        left_lines = cleaned_lines[::2]
        right_lines = cleaned_lines[1::2]
        row_count = max(len(left_lines), len(right_lines))

        for row_index in range(row_count):
            left_text = left_lines[row_index] if row_index < len(left_lines) else ""
            right_text = right_lines[row_index] if row_index < len(right_lines) else ""

            left_wrapped = split_text_to_lines(left_text, "Times-Roman", 10.2, column_width - 14) if left_text else []
            right_wrapped = split_text_to_lines(right_text, "Times-Roman", 10.2, column_width - 14) if right_text else []
            row_height = max(len(left_wrapped), len(right_wrapped), 1) * 12
            ensure_space(row_height)
            p.setFont("Times-Roman", 10.2)

            for index, line in enumerate(left_wrapped):
                current_y = y - (index * 12)
                if index == 0:
                    p.drawString(left_margin, current_y, u"\u2022")
                p.drawString(left_margin + 10, current_y, line)

            right_x = left_margin + column_width + column_gap
            for index, line in enumerate(right_wrapped):
                current_y = y - (index * 12)
                if index == 0:
                    p.drawString(right_x, current_y, u"\u2022")
                p.drawString(right_x + 10, current_y, line)

            y -= row_height

    def parse_structured_text(text):
        sections = []
        current = None
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("- "):
                if current is None:
                    current = {"header": "", "bullets": []}
                current["bullets"].append(line)
            else:
                if current:
                    sections.append(current)
                current = {"header": line, "bullets": []}
        if current:
            sections.append(current)
        return sections

    def draw_entry_sections(section_text):
        nonlocal y
        for entry in parse_structured_text(section_text):
            header = entry["header"]
            bullets = entry["bullets"]
            if header:
                header_parts = [part.strip() for part in header.split("|")]
                title = header_parts[0] if header_parts else ""
                details = " | ".join(header_parts[1:]) if len(header_parts) > 1 else ""
                draw_wrapped_line(title, font_name="Times-Bold", font_size=10.5, leading=12)
                if details:
                    draw_wrapped_line(details, font_name="Times-Italic", font_size=10, leading=12)
            draw_bullet_list(bullets)
            y -= 3

    p.setFont("Times-Bold", 18)
    p.drawCentredString(page_width / 2, y, profile.get("name", "Your Name"))
    y -= 20

    contact = " | ".join(filter(None, [
        profile.get("email"),
        profile.get("phone"),
        profile.get("linkedin"),
        profile.get("portfolio")
    ]))
    if contact:
        for line in split_text_to_lines(contact, "Times-Roman", 10.5, content_width):
            p.setFont("Times-Roman", 10.5)
            p.drawCentredString(page_width / 2, y, line)
            y -= 13
    y -= 8

    draw_section_title("EDUCATION")
    ensure_space(28)
    p.setFont("Times-Italic", 10.5)
    p.drawString(left_margin, y, education_school)
    if education_grad:
        grad_text = f"Graduation {education_grad}"
        p.drawRightString(page_width - right_margin, y, grad_text)
    y -= 13
    if education_degree:
        p.setFont("Times-Roman", 10.5)
        p.drawString(left_margin + 12, y, education_degree)
        y -= 16

    draw_section_title("TECHNICAL SKILLS")
    draw_two_column_skills(skills_lines)
    y -= 4

    draw_section_title("PROJECTS")
    draw_entry_sections(tailored_projects)

    draw_section_title("WORK EXPERIENCE")
    draw_entry_sections(tailored_experience)

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

    resume_folder_names = {
        resume.get("folder", "Saved Drafts")
        for resume in all_resumes
        if resume.get("folder")
    }
    created_folder_names = {
        folder.get("name", "").strip()
        for folder in folders_collection.find({"user_id": user_id})
        if folder.get("name")
    }
    folder_names = sorted(resume_folder_names | created_folder_names)

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


@app.route("/create-folder", methods=["POST"])
def create_folder():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    folder_name = request.form.get("folder_name", "").strip()

    if not folder_name:
        return redirect(url_for("resumes"))

    folders_collection.update_one(
        {"user_id": user_id, "name": folder_name},
        {"$setOnInsert": {
            "user_id": user_id,
            "name": folder_name,
            "created_at": datetime.utcnow()
        }},
        upsert=True
    )

    return redirect(url_for("resumes", folder=folder_name))

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
