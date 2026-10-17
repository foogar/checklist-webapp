# Collaborative Checklist PWA

A self-hosted, mobile-friendly collaborative checklist web application built with FastAPI, SQLite, Tailwind CSS, and Alpine.js, featuring PWA support.

## Features
- PWA Compliant for full-screen mobile usage
- Secure Google OAuth authentication
- Account sharing by email
- Daily reset mode and task reordering

## Setup
1. Clone the repository
2. Create a virtual environment and run `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your Google OAuth credentials
4. Run with `uvicorn main:app --host 0.0.0.0 --port 8000`
