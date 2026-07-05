"""
AI Resume Analyzer
------------------
A Streamlit application that parses resumes, predicts career fields,
recommends skills and courses, and provides an admin dashboard.
"""

import os
import io
import socket
import platform
import secrets
import base64
import random
import time
import datetime

import streamlit as st
import pandas as pd
import pymysql
import geocoder
import plotly.express as px
from geopy.geocoders import Nominatim
from pyresparser import ResumeParser
from pdfminer3.layout import LAParams
from pdfminer3.pdfpage import PDFPage
from pdfminer3.pdfinterp import PDFResourceManager, PDFPageInterpreter
from pdfminer3.converter import TextConverter
from streamlit_tags import st_tags
from PIL import Image
import nltk

from courses import (
    ds_course, web_course, android_course,
    ios_course, uiux_course, resume_videos, interview_videos,
)

nltk.download('stopwords', quiet=True)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_connection():
    """Return a fresh pymysql connection using env vars (with fallback defaults)."""
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        db=os.getenv("DB_NAME", "resume_analyzer_db"),
    )


def ensure_schema(cursor):
    """Create database and tables if they do not already exist."""
    cursor.execute("CREATE DATABASE IF NOT EXISTS resume_analyzer_db;")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_data (
            id              INT NOT NULL AUTO_INCREMENT,
            sec_token       VARCHAR(20) NOT NULL,
            ip_address      VARCHAR(50),
            host_name       VARCHAR(50),
            device_user     VARCHAR(50),
            os_info         VARCHAR(50),
            lat_long        VARCHAR(50),
            city            VARCHAR(50),
            state           VARCHAR(50),
            country         VARCHAR(50),
            actual_name     VARCHAR(50) NOT NULL,
            actual_email    VARCHAR(50) NOT NULL,
            actual_mobile   VARCHAR(20) NOT NULL,
            parsed_name     VARCHAR(500) NOT NULL,
            parsed_email    VARCHAR(500) NOT NULL,
            resume_score    VARCHAR(8) NOT NULL,
            created_at      VARCHAR(50) NOT NULL,
            page_count      VARCHAR(5) NOT NULL,
            predicted_field BLOB NOT NULL,
            experience_level BLOB NOT NULL,
            current_skills  BLOB NOT NULL,
            suggested_skills BLOB NOT NULL,
            suggested_courses BLOB NOT NULL,
            filename        VARCHAR(100) NOT NULL,
            PRIMARY KEY (id)
        );
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_feedback (
            id          INT NOT NULL AUTO_INCREMENT,
            name        VARCHAR(50) NOT NULL,
            email       VARCHAR(50) NOT NULL,
            score       VARCHAR(5) NOT NULL,
            comments    VARCHAR(255),
            created_at  VARCHAR(50) NOT NULL,
            PRIMARY KEY (id)
        );
    """)


def save_user_record(cursor, connection, record: dict):
    sql = """
        INSERT INTO user_data VALUES (
            0, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
    """
    cursor.execute(sql, (
        record["sec_token"], record["ip_address"], record["host_name"],
        record["device_user"], record["os_info"], record["lat_long"],
        record["city"], record["state"], record["country"],
        record["actual_name"], record["actual_email"], record["actual_mobile"],
        record["parsed_name"], record["parsed_email"], record["resume_score"],
        record["created_at"], record["page_count"], record["predicted_field"],
        record["experience_level"], record["current_skills"],
        record["suggested_skills"], record["suggested_courses"],
        record["filename"],
    ))
    connection.commit()


def save_feedback(cursor, connection, name, email, score, comments, timestamp):
    sql = "INSERT INTO user_feedback VALUES (0, %s, %s, %s, %s, %s)"
    cursor.execute(sql, (name, email, score, comments, timestamp))
    connection.commit()


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def extract_text_from_pdf(file_path: str) -> str:
    """Return plain text extracted from a PDF file."""
    resource_mgr = PDFResourceManager()
    buffer = io.StringIO()
    converter = TextConverter(resource_mgr, buffer, laparams=LAParams())
    interpreter = PDFPageInterpreter(resource_mgr, converter)

    with open(file_path, "rb") as fh:
        for page in PDFPage.get_pages(fh, caching=True, check_extractable=True):
            interpreter.process_page(page)

    text = buffer.getvalue()
    converter.close()
    buffer.close()
    return text


def render_pdf_inline(file_path: str):
    """Embed a PDF in the Streamlit page via an iframe."""
    with open(file_path, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("utf-8")
    iframe = (
        f'<iframe src="data:application/pdf;base64,{encoded}" '
        'width="700" height="1000" type="application/pdf"></iframe>'
    )
    st.markdown(iframe, unsafe_allow_html=True)


def csv_download_link(df: pd.DataFrame, filename: str, link_text: str) -> str:
    csv_bytes = base64.b64encode(df.to_csv(index=False).encode()).decode()
    return f'<a href="data:file/csv;base64,{csv_bytes}" download="{filename}">{link_text}</a>'


# ---------------------------------------------------------------------------
# Course recommender widget
# ---------------------------------------------------------------------------

def recommend_courses(course_list: list) -> list:
    st.subheader("📚 Recommended Courses & Certificates")
    count = st.slider("How many recommendations would you like?", 1, 10, 5)
    random.shuffle(course_list)
    selected = []
    for idx, (name, url) in enumerate(course_list[:count], start=1):
        st.markdown(f"({idx}) [{name}]({url})")
        selected.append(name)
    return selected


# ---------------------------------------------------------------------------
# Experience-level detector
# ---------------------------------------------------------------------------

def detect_experience_level(resume_text: str, page_count: int) -> str:
    text_upper = resume_text.upper()
    if page_count < 1:
        return "Fresher"
    if "INTERNSHIP" in text_upper or "INTERNSHIPS" in text_upper:
        return "Intermediate"
    if "EXPERIENCE" in text_upper or "WORK EXPERIENCE" in text_upper:
        return "Experienced"
    return "Fresher"


_LEVEL_COLORS = {
    "Fresher": "#d73b5c",
    "Intermediate": "#1ed760",
    "Experienced": "#fba171",
}


def show_experience_level(level: str):
    color = _LEVEL_COLORS.get(level, "#ffffff")
    st.markdown(
        f"<h4 style='text-align:left;color:{color};'>Experience Level: {level}</h4>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Skill-field mapping
# ---------------------------------------------------------------------------

FIELD_KEYWORDS = {
    "Data Science": {
        "keywords": {"tensorflow", "keras", "pytorch", "machine learning",
                     "deep learning", "flask", "streamlit"},
        "suggested_skills": [
            "Data Visualization", "Predictive Analysis", "Statistical Modeling",
            "Data Mining", "Clustering & Classification", "Data Analytics",
            "Quantitative Analysis", "Web Scraping", "ML Algorithms",
            "Keras", "Pytorch", "Scikit-learn", "Tensorflow", "Flask", "Streamlit",
        ],
        "courses": "ds_course",
    },
    "Web Development": {
        "keywords": {"react", "django", "node js", "react js", "php", "laravel",
                     "magento", "wordpress", "javascript", "angular js", "c#",
                     "asp.net", "flask"},
        "suggested_skills": [
            "React", "Django", "Node JS", "PHP", "Laravel", "WordPress",
            "JavaScript", "Angular JS", "C#", "Flask", "SDK",
        ],
        "courses": "web_course",
    },
    "Android Development": {
        "keywords": {"android", "android development", "flutter", "kotlin",
                     "xml", "kivy"},
        "suggested_skills": [
            "Android", "Flutter", "Kotlin", "XML", "Java", "Kivy",
            "Git", "SDK", "SQLite",
        ],
        "courses": "android_course",
    },
    "iOS Development": {
        "keywords": {"ios", "ios development", "swift", "cocoa",
                     "cocoa touch", "xcode"},
        "suggested_skills": [
            "iOS", "Swift", "Cocoa", "Xcode", "Objective-C", "SQLite",
            "StoreKit", "UIKit", "AVFoundation", "Auto-Layout",
        ],
        "courses": "ios_course",
    },
    "UI/UX Development": {
        "keywords": {"ux", "adobe xd", "figma", "zeplin", "balsamiq",
                     "ui", "prototyping", "wireframes", "adobe photoshop",
                     "photoshop", "illustrator", "after effects",
                     "premier pro", "indesign", "user research"},
        "suggested_skills": [
            "UI", "User Experience", "Adobe XD", "Figma", "Zeplin",
            "Balsamiq", "Prototyping", "Wireframes", "Photoshop",
            "Illustrator", "After Effects", "Indesign", "User Research",
        ],
        "courses": "uiux_course",
    },
}

COURSE_MAP = {
    "ds_course": ds_course,
    "web_course": web_course,
    "android_course": android_course,
    "ios_course": ios_course,
    "uiux_course": uiux_course,
}


def predict_field_and_recommend(skills: list):
    """
    Given a list of skills return (field, suggested_skills, rec_courses).
    Returns (None, [], []) if no field is matched.
    """
    for skill in skills:
        skill_lower = skill.lower()
        for field, data in FIELD_KEYWORDS.items():
            if skill_lower in data["keywords"]:
                suggested = data["suggested_skills"]
                courses = recommend_courses(COURSE_MAP[data["courses"]])
                return field, suggested, courses

    return None, [], []


# ---------------------------------------------------------------------------
# Resume scorer
# ---------------------------------------------------------------------------

SCORE_CRITERIA = [
    ({"objective", "summary"}, 6, "Objective / Summary"),
    ({"education", "school", "college"}, 12, "Education Details"),
    ({"experience", "work experience"}, 16, "Work Experience"),
    ({"internship", "internships"}, 6, "Internships"),
    ({"skills", "skill"}, 7, "Skills Section"),
    ({"hobbies"}, 4, "Hobbies"),
    ({"interests"}, 5, "Interests"),
    ({"achievements"}, 13, "Achievements"),
    ({"certifications", "certification"}, 12, "Certifications"),
    ({"projects", "project"}, 19, "Projects"),
]


def compute_resume_score(resume_text: str) -> int:
    """Score the resume out of 100 based on section presence."""
    text_lower = resume_text.lower()
    score = 0
    st.subheader("📝 Resume Tips & Score")

    for keywords, points, label in SCORE_CRITERIA:
        found = any(kw in text_lower for kw in keywords)
        if found:
            score += points
            st.markdown(
                f"<h5 style='color:#1ed760;'>[✔] {label} detected — +{points} pts</h5>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"<h5 style='color:#000000;'>[✘] Consider adding a <b>{label}</b> section.</h5>",
                unsafe_allow_html=True,
            )

    return score


# ---------------------------------------------------------------------------
# Geo helpers
# ---------------------------------------------------------------------------

def get_location_info():
    """Return (lat_long, city, state, country) based on the current IP."""
    try:
        g = geocoder.ip("me")
        lat_long = g.latlng
        geolocator = Nominatim(user_agent="resume-analyzer")
        location = geolocator.reverse(lat_long, language="en")
        addr = location.raw.get("address", {})
        return (
            str(lat_long),
            addr.get("city", ""),
            addr.get("state", ""),
            addr.get("country", ""),
        )
    except Exception:
        return ("N/A", "N/A", "N/A", "N/A")


# ---------------------------------------------------------------------------
# Page sections
# ---------------------------------------------------------------------------

def user_section(cursor, connection):
    st.markdown(
        "<h5 style='color:#021659;'>Upload your resume and get smart recommendations</h5>",
        unsafe_allow_html=True,
    )

    actual_name = st.text_input("Your Name *")
    actual_email = st.text_input("Your Email *")
    actual_mobile = st.text_input("Your Mobile *")

    pdf_file = st.file_uploader("Upload Resume (PDF only)", type=["pdf"])
    if pdf_file is None:
        return

    with st.spinner("Analyzing your resume…"):
        time.sleep(2)

    save_path = os.path.join("Uploaded_Resumes", pdf_file.name)
    with open(save_path, "wb") as f:
        f.write(pdf_file.getbuffer())

    render_pdf_inline(save_path)

    resume_data = ResumeParser(save_path).get_extracted_data()
    if not resume_data:
        st.error("Could not parse the resume. Please upload a text-based PDF.")
        return

    resume_text = extract_text_from_pdf(save_path)

    # --- Basic info ---
    st.header("📊 Resume Analysis")
    st.success(f"Hello, {resume_data.get('name', 'Candidate')}!")
    st.subheader("Basic Information")
    for label, key in [("Name", "name"), ("Email", "email"),
                       ("Phone", "mobile_number"), ("Degree", "degree"),
                       ("Pages", "no_of_pages")]:
        value = resume_data.get(key, "N/A")
        if value:
            st.text(f"{label}: {value}")

    # --- Experience level ---
    page_count = resume_data.get("no_of_pages", 0)
    level = detect_experience_level(resume_text, page_count)
    show_experience_level(level)

    # --- Skills & recommendations ---
    st.subheader("💡 Skills Recommendation")
    st_tags(
        label="Your current skills",
        text="Skills found in your resume",
        value=resume_data.get("skills", []),
        key="current_skills",
    )

    field, suggested_skills, rec_courses = predict_field_and_recommend(
        resume_data.get("skills", [])
    )

    if field:
        st.success(f"Our analysis suggests you are suited for **{field}** roles.")
        st_tags(
            label="Recommended skills to add",
            text="Adding these can boost your profile",
            value=suggested_skills,
            key="suggested_skills",
        )
        st.markdown(
            "<h5 style='color:#1ed760;'>Adding these skills can significantly boost your job prospects 🚀</h5>",
            unsafe_allow_html=True,
        )
    else:
        st.warning(
            "We could not predict a specific field. "
            "Currently supported: Data Science, Web, Android, iOS, UI/UX."
        )
        suggested_skills = []
        rec_courses = []

    # --- Resume score ---
    resume_score = compute_resume_score(resume_text)

    st.subheader("🏆 Resume Score")
    st.markdown(
        "<style>.stProgress > div > div > div > div {background-color: #d73b5c;}</style>",
        unsafe_allow_html=True,
    )
    progress_bar = st.progress(0)
    for i in range(resume_score):
        time.sleep(0.05)
        progress_bar.progress(i + 1)

    st.success(f"Your resume writing score: **{resume_score} / 100**")
    st.warning("Score is based on the sections present in your resume.")

    # --- Bonus videos ---
    st.header("🎬 Resume Writing Tips (Video)")
    st.video(random.choice(resume_videos))

    st.header("🎬 Interview Preparation Tips (Video)")
    st.video(random.choice(interview_videos))

    # --- Save to DB ---
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")
    lat_long, city, state, country = get_location_info()

    record = {
        "sec_token": secrets.token_urlsafe(12),
        "ip_address": socket.gethostbyname(socket.gethostname()),
        "host_name": socket.gethostname(),
        "device_user": os.getlogin(),
        "os_info": f"{platform.system()} {platform.release()}",
        "lat_long": lat_long,
        "city": city,
        "state": state,
        "country": country,
        "actual_name": actual_name,
        "actual_email": actual_email,
        "actual_mobile": actual_mobile,
        "parsed_name": resume_data.get("name", ""),
        "parsed_email": resume_data.get("email", ""),
        "resume_score": str(resume_score),
        "created_at": ts,
        "page_count": str(page_count),
        "predicted_field": field or "N/A",
        "experience_level": level,
        "current_skills": str(resume_data.get("skills", [])),
        "suggested_skills": str(suggested_skills),
        "suggested_courses": str(rec_courses),
        "filename": pdf_file.name,
    }
    try:
        save_user_record(cursor, connection, record)
    except Exception as e:
        st.warning(f"Could not save record to database: {e}")

    st.balloons()


def feedback_section(cursor, connection):
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H:%M:%S")

    with st.form("feedback_form"):
        st.subheader("📝 Share Your Feedback")
        name = st.text_input("Name")
        email = st.text_input("Email")
        score = st.slider("Rate the tool (1–5)", 1, 5, 3)
        comments = st.text_input("Comments")
        submitted = st.form_submit_button("Submit")

        if submitted:
            try:
                save_feedback(cursor, connection, name, email, score, comments, ts)
                st.success("Thank you! Your feedback has been recorded.")
                st.balloons()
            except Exception as e:
                st.error(f"Could not save feedback: {e}")

    # Show aggregated ratings
    try:
        feed_df = pd.read_sql("SELECT * FROM user_feedback", connection)
        if not feed_df.empty:
            st.subheader("📊 Feedback Summary")
            fig = px.pie(
                values=feed_df["score"].value_counts(),
                names=feed_df["score"].unique(),
                title="User Rating Distribution (1–5)",
                color_discrete_sequence=px.colors.sequential.Aggrnyl,
            )
            st.plotly_chart(fig)

            cursor.execute("SELECT name, comments FROM user_feedback")
            comments_data = cursor.fetchall()
            st.subheader("💬 User Comments")
            st.dataframe(pd.DataFrame(comments_data, columns=["User", "Comment"]), width=1000)
    except Exception:
        pass


def about_section():
    st.subheader("About – AI Resume Analyzer")
    st.markdown("""
**AI Resume Analyzer** is an open-source tool that uses natural language processing to
parse resumes, identify candidate skills, predict suitable career fields, and
provide personalized recommendations for skills and learning resources.

---

**How to use:**

- **User** – Select *User* from the sidebar, fill in your details, and upload a PDF resume.
  The tool will analyze it and give you instant feedback.

- **Feedback** – Submit ratings and comments to help improve the tool.

- **Admin** – Log in with your configured credentials to view analytics and user data.

---

**Tech stack:** Python · Streamlit · spaCy · pdfminer3 · pyresparser · MySQL · Plotly
""")


def admin_section(cursor, connection):
    st.subheader("🔐 Admin Login")

    admin_user = os.getenv("ADMIN_USER", "admin")
    admin_pass = os.getenv("ADMIN_PASSWORD", "changeme")

    username = st.text_input("Username")
    password = st.text_input("Password", type="password")

    if not st.button("Login"):
        return

    if username != admin_user or password != admin_pass:
        st.error("Invalid credentials.")
        return

    st.success("Welcome, Admin!")

    # User data table
    try:
        cursor.execute("""
            SELECT id, ip_address, resume_score,
                   CONVERT(predicted_field USING utf8),
                   CONVERT(experience_level USING utf8),
                   city, state, country
            FROM user_data
        """)
        plot_data = pd.DataFrame(
            cursor.fetchall(),
            columns=["ID", "IP", "Score", "Field", "Level", "City", "State", "Country"],
        )

        cursor.execute("""
            SELECT id, sec_token, ip_address, actual_name, actual_email, actual_mobile,
                   CONVERT(predicted_field USING utf8), created_at, parsed_name,
                   parsed_email, resume_score, page_count, filename,
                   CONVERT(experience_level USING utf8),
                   CONVERT(current_skills USING utf8),
                   CONVERT(suggested_skills USING utf8),
                   CONVERT(suggested_courses USING utf8),
                   city, state, country, lat_long, os_info, host_name, device_user
            FROM user_data
        """)
        full_data = pd.DataFrame(
            cursor.fetchall(),
            columns=[
                "ID", "Token", "IP", "Name", "Email", "Mobile", "Predicted Field",
                "Timestamp", "Parsed Name", "Parsed Email", "Resume Score", "Pages",
                "Filename", "Level", "Current Skills", "Suggested Skills",
                "Suggested Courses", "City", "State", "Country", "LatLong",
                "OS", "Host", "Device User",
            ],
        )

        st.header("📋 User Records")
        st.dataframe(full_data)
        st.markdown(
            csv_download_link(full_data, "user_data.csv", "⬇ Download CSV"),
            unsafe_allow_html=True,
        )

        # Charts
        def pie_chart(values, names, title, color_seq):
            fig = px.pie(values=values, names=names, title=title,
                         color_discrete_sequence=color_seq)
            st.plotly_chart(fig)

        st.subheader("📊 Analytics")

        pie_chart(
            plot_data["Field"].value_counts(),
            plot_data["Field"].unique(),
            "Predicted Career Fields",
            px.colors.sequential.Aggrnyl_r,
        )
        pie_chart(
            plot_data["Level"].value_counts(),
            plot_data["Level"].unique(),
            "Experience Levels",
            px.colors.sequential.RdBu,
        )
        pie_chart(
            plot_data["Score"].value_counts(),
            plot_data["Score"].unique(),
            "Resume Scores",
            px.colors.sequential.Agsunset,
        )
        pie_chart(
            plot_data["City"].value_counts(),
            plot_data["City"].unique(),
            "Usage by City",
            px.colors.sequential.Jet,
        )
        pie_chart(
            plot_data["Country"].value_counts(),
            plot_data["Country"].unique(),
            "Usage by Country",
            px.colors.sequential.Purpor_r,
        )

    except Exception as e:
        st.error(f"Error loading admin data: {e}")

    # Feedback table
    try:
        cursor.execute("SELECT * FROM user_feedback")
        feed_df = pd.DataFrame(
            cursor.fetchall(),
            columns=["ID", "Name", "Email", "Score", "Comments", "Timestamp"],
        )
        st.header("📝 Feedback Records")
        st.dataframe(feed_df)
    except Exception as e:
        st.warning(f"Could not load feedback data: {e}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title="AI Resume Analyzer",
        page_icon="./Logo/recommend.png",
    )

    logo = Image.open("./Logo/RESUM.png")
    st.image(logo)

    st.sidebar.markdown("## Navigation")
    page = st.sidebar.selectbox(
        "Go to",
        ["User", "Feedback", "About", "Admin"],
    )

    # DB setup
    connection = get_connection()
    cursor = connection.cursor()
    ensure_schema(cursor)

    if page == "User":
        user_section(cursor, connection)
    elif page == "Feedback":
        feedback_section(cursor, connection)
    elif page == "About":
        about_section()
    else:
        admin_section(cursor, connection)


if __name__ == "__main__":
    main()
