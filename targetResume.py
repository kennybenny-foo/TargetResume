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
    profile = profiles_collection.find_one({"user_id": user_id})

    return render_template("dashboard.html", profile=profile)


@app.route("/generate-resume-preview", methods=["POST"])
def generate_resume_preview():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    user_id = session["user_id"]
    profile = profiles_collection.find_one({"user_id": user_id}) or {}

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
        "skills": profile.get("skills", ""),
        "projects": profile.get("projects", ""),
        "experience": profile.get("experience", "")
    }

    return jsonify(response)

@app.route("/ai-rewrite-preview", methods=["POST"])
def ai_rewrite_preview():
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    if not openai_client:
        return jsonify({"error": "OPENAI_API_KEY is not set."}), 500