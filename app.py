
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, session, send_from_directory, jsonify
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import os
import uuid
try:
    import pymysql
except ModuleNotFoundError as exc:
    pymysql = None
    PYMYSQL_IMPORT_ERROR = exc
else:
    PYMYSQL_IMPORT_ERROR = None
try:
    import dotenv
except ModuleNotFoundError:
    dotenv = None
from datetime import datetime

if dotenv is not None:
    dotenv.load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-secret-key")

UPLOAD_FOLDER = os.path.join(app.root_path, "uploads")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
REPORT_COOLDOWN_SECONDS = 120


# ==================================================
# DATABASE CONNECTION
# ==================================================

def get_db_connection():
    if pymysql is None:
        raise RuntimeError(
            "PyMySQL is not installed. Install it with 'pip install PyMySQL'."
        ) from PYMYSQL_IMPORT_ERROR

    required = ["MYSQLHOST", "MYSQLUSER", "MYSQLPASSWORD", "MYSQLDATABASE"]
    missing = [key for key in required if not os.getenv(key)]

    if missing:
        raise RuntimeError(
            "Missing database environment variables: " + ", ".join(missing)
        )

    return pymysql.connect(
        host=os.getenv("MYSQLHOST"),
        port=int(os.getenv("MYSQLPORT", "3306")),
        user=os.getenv("MYSQLUSER"),
        password=os.getenv("MYSQLPASSWORD"),
        database=os.getenv("MYSQLDATABASE"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        autocommit=False
    )


def init_db():
    """Repair users.id and create the reports/notifications tables."""
    connection = get_db_connection()

    try:
        with connection.cursor() as cursor:
            # The users table must already exist.
            cursor.execute("SHOW COLUMNS FROM users")
            columns = cursor.fetchall()

            id_column = next(
                (column for column in columns if column["Field"] == "id"),
                None
            )

            if not id_column:
                raise RuntimeError(
                    "The users table must contain an integer id column."
                )

            # AUTO_INCREMENT requires an index.
            cursor.execute("SHOW INDEX FROM users")
            indexes = cursor.fetchall()

            id_is_indexed = any(
                index["Column_name"] == "id" for index in indexes
            )
            has_primary_key = any(
                index["Key_name"] == "PRIMARY" for index in indexes
            )

            if not id_is_indexed:
                if not has_primary_key:
                    cursor.execute(
                        "ALTER TABLE users ADD PRIMARY KEY (id)"
                    )
                else:
                    cursor.execute("""
                        ALTER TABLE users
                        ADD UNIQUE KEY id_auto_increment_unique (id)
                    """)

            cursor.execute("""
                ALTER TABLE users
                MODIFY COLUMN id INT NOT NULL AUTO_INCREMENT
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS reports (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    report_code VARCHAR(40) NOT NULL UNIQUE,
                    resident_email VARCHAR(255) NOT NULL,
                    fullname VARCHAR(150) NOT NULL,
                    report_date VARCHAR(30) NOT NULL,
                    report_time VARCHAR(30) NOT NULL,
                    sitio VARCHAR(255) NOT NULL,
                    description TEXT,
                    image VARCHAR(255) NOT NULL,
                    clean_image VARCHAR(255) DEFAULT NULL,
                    priority VARCHAR(20) NOT NULL DEFAULT 'Normal',
                    status VARCHAR(30) NOT NULL DEFAULT 'Pending',
                    created_at DATETIME NOT NULL,
                    completed_at DATETIME DEFAULT NULL,
                    INDEX idx_report_email (resident_email),
                    INDEX idx_report_status (status),
                    INDEX idx_report_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS notifications (
                    id VARCHAR(64) PRIMARY KEY,
                    title VARCHAR(200) NOT NULL,
                    message TEXT NOT NULL,
                    type VARCHAR(50) NOT NULL,
                    report_id VARCHAR(40) DEFAULT NULL,
                    created_at DATETIME NOT NULL,
                    is_read TINYINT(1) NOT NULL DEFAULT 0,
                    INDEX idx_notification_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)

        connection.commit()

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def db_fetchone(query, params=()):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchone()
    finally:
        connection.close()


def db_fetchall(query, params=()):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            return cursor.fetchall()
    finally:
        connection.close()


def db_execute(query, params=()):
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


# ==================================================
# HELPERS
# ==================================================

def is_ajax_request():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def allowed_file(filename):
    return (
        bool(filename)
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def parse_datetime(value):
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    return datetime.strptime(str(value), DATE_FORMAT)


def serialize_report(row):
    if not row:
        return None

    created_at = parse_datetime(row["created_at"])
    completed_at = parse_datetime(row.get("completed_at"))

    return {
        "id": row["report_code"],
        "email": row["resident_email"],
        "fullname": row["fullname"],
        "date": row["report_date"],
        "time": row["report_time"],
        "sitio": row["sitio"],
        "description": row.get("description") or "",
        "image": row["image"],
        "clean_image": row.get("clean_image"),
        "priority": row.get("priority") or "Normal",
        "status": row["status"],
        "created_at": created_at.strftime(DATE_FORMAT),
        "completed_at": (
            completed_at.strftime(DATE_FORMAT) if completed_at else None
        )
    }


def get_all_reports():
    rows = db_fetchall(
        "SELECT * FROM reports ORDER BY created_at ASC, id ASC"
    )
    return [serialize_report(row) for row in rows]


def get_report_by_id(report_id):
    row = db_fetchone(
        "SELECT * FROM reports WHERE report_code = %s LIMIT 1",
        (report_id,)
    )
    return serialize_report(row)


def get_all_notifications():
    rows = db_fetchall(
        "SELECT * FROM notifications ORDER BY created_at DESC"
    )
    result = []

    for row in rows:
        created_at = parse_datetime(row["created_at"])
        result.append({
            "id": row["id"],
            "title": row["title"],
            "message": row["message"],
            "type": row["type"],
            "report_id": row.get("report_id"),
            "created_at": created_at.strftime(DATE_FORMAT),
            "is_read": bool(row["is_read"])
        })

    return result


def create_notification(title, message, notification_type, report_id=None):
    db_execute("""
        INSERT INTO notifications
            (id, title, message, type, report_id, created_at, is_read)
        VALUES (%s, %s, %s, %s, %s, %s, 0)
    """, (
        uuid.uuid4().hex,
        title,
        message,
        notification_type,
        report_id,
        datetime.now().strftime(DATE_FORMAT)
    ))


def get_notification_counts():
    counts = db_fetchone("""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END), 0) AS unread
        FROM notifications
    """)
    return {
        "total": int(counts["total"]),
        "unread": int(counts["unread"])
    }


def get_report_duration(report):
    created_at = datetime.strptime(report["created_at"], DATE_FORMAT)

    if report["status"] == "Completed" and report.get("completed_at"):
        end_time = datetime.strptime(report["completed_at"], DATE_FORMAT)
    else:
        end_time = datetime.now()

    seconds = max(0, int((end_time - created_at).total_seconds()))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    if days:
        return f"{days} day(s), {hours} hour(s)"
    if hours:
        return f"{hours} hour(s), {minutes} min"
    return f"{minutes} minute(s)"


def is_urgent(report):
    if report["status"] != "Pending":
        return False
    if report.get("priority") == "Urgent":
        return True

    created_at = datetime.strptime(report["created_at"], DATE_FORMAT)
    return (datetime.now() - created_at).total_seconds() >= 86400


def prepare_report(report):
    result = report.copy()
    result["duration"] = get_report_duration(report)
    result["is_urgent"] = is_urgent(report)
    result["priority"] = report.get("priority", "Normal")
    return result


def get_dashboard_counts():
    reports = get_all_reports()
    return {
        "total_reports": len(reports),
        "pending_reports": sum(r["status"] == "Pending" for r in reports),
        "in_progress_reports": sum(
            r["status"] == "In Progress" for r in reports
        ),
        "completed_reports": sum(
            r["status"] == "Completed" for r in reports
        ),
        "urgent_reports": sum(is_urgent(r) for r in reports)
    }


def action_response(success, message, report=None, status_code=200):
    if is_ajax_request():
        data = {"success": success, "message": message}
        if report is not None:
            data["report"] = prepare_report(report)
        try:
            data["urgent_reports"] = get_dashboard_counts()["urgent_reports"]
        except Exception:
            data["urgent_reports"] = 0
        return jsonify(data), status_code

    flash(message)
    return redirect(url_for("admin_dashboard"))


def report_error(message, status_code=400):
    if is_ajax_request():
        return jsonify({"success": False, "message": message}), status_code
    flash(message)
    return redirect(url_for("resident_home"))


# ==================================================
# HOME AND LOGIN PAGES
# ==================================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/login")
def login():
    return render_template("login.html")


@app.route("/admin-login")
def admin_login():
    return render_template("admin_login.html")


# ==================================================
# ADMIN DASHBOARD AND REPORT API
# ==================================================

@app.route("/admin-dashboard")
def admin_dashboard():
    try:
        reports = [prepare_report(r) for r in get_all_reports()]
        counts = get_dashboard_counts()
        notification_counts = get_notification_counts()
    except Exception:
        app.logger.exception("Admin dashboard database error")
        flash("Could not load dashboard. Check the database connection.")
        reports = []
        counts = {
            "total_reports": 0,
            "pending_reports": 0,
            "in_progress_reports": 0,
            "completed_reports": 0,
            "urgent_reports": 0
        }
        notification_counts = {"total": 0, "unread": 0}

    return render_template(
        "admin_dashboard.html",
        reports=reports,
        total_reports=counts["total_reports"],
        pending_reports=counts["pending_reports"],
        in_progress_reports=counts["in_progress_reports"],
        completed_reports=counts["completed_reports"],
        urgent_reports=counts["urgent_reports"],
        notification_counts=notification_counts
    )


@app.route("/api/admin/reports")
def admin_reports_api():
    try:
        return jsonify({
            "reports": [prepare_report(r) for r in get_all_reports()],
            **get_dashboard_counts()
        })
    except Exception:
        app.logger.exception("Admin reports API error")
        return jsonify({
            "success": False,
            "message": "Could not load reports."
        }), 500


# ==================================================
# ADMIN NOTIFICATIONS
# ==================================================

@app.route("/api/admin/notifications")
def admin_notifications_api():
    try:
        counts = get_notification_counts()
        return jsonify({
            "success": True,
            "notifications": get_all_notifications(),
            "total": counts["total"],
            "unread": counts["unread"]
        })
    except Exception:
        app.logger.exception("Notification API error")
        return jsonify({
            "success": False,
            "message": "Could not load notifications."
        }), 500


@app.route("/api/admin/notifications/<notification_id>/read", methods=["POST"])
def mark_notification_read(notification_id):
    try:
        exists = db_fetchone(
            "SELECT id FROM notifications WHERE id = %s",
            (notification_id,)
        )
        if not exists:
            return jsonify({
                "success": False,
                "message": "Notification not found."
            }), 404

        db_execute(
            "UPDATE notifications SET is_read = 1 WHERE id = %s",
            (notification_id,)
        )
        return jsonify({
            "success": True,
            "unread": get_notification_counts()["unread"]
        })
    except Exception:
        app.logger.exception("Mark notification read error")
        return jsonify({
            "success": False,
            "message": "Could not update notification."
        }), 500


@app.route("/api/admin/notifications/read-all", methods=["POST"])
def mark_all_notifications_read():
    try:
        db_execute("UPDATE notifications SET is_read = 1")
        return jsonify({"success": True, "unread": 0})
    except Exception:
        app.logger.exception("Mark all notifications read error")
        return jsonify({
            "success": False,
            "message": "Could not update notifications."
        }), 500


# ==================================================
# ADMIN: START CLEANING
# ==================================================

@app.route("/start-cleaning/<report_id>", methods=["POST"])
def start_cleaning(report_id):
    try:
        report = get_report_by_id(report_id)
        if report is None:
            return action_response(False, "Report not found.", status_code=404)

        if report["status"] == "Completed":
            return action_response(
                False, "This report is already completed.", report, 400
            )

        if report["status"] == "In Progress":
            return action_response(
                True, "This report is already being cleaned.", report
            )

        db_execute(
            "UPDATE reports SET status = %s WHERE report_code = %s",
            ("In Progress", report_id)
        )
        report["status"] = "In Progress"

        create_notification(
            "Cleaning Started",
            f"Cleaning has started for report {report_id}.",
            "in_progress",
            report_id
        )
        return action_response(
            True, "Report status updated to In Progress!", report
        )
    except Exception:
        app.logger.exception("Start cleaning error")
        return action_response(
            False, "Could not update report. Check the database.", status_code=500
        )


# ==================================================
# ADMIN: COMPLETE REPORT
# ==================================================

@app.route("/complete-report/<report_id>", methods=["POST"])
def complete_report(report_id):
    try:
        report = get_report_by_id(report_id)
        if report is None:
            return action_response(False, "Report not found.", status_code=404)

        if report["status"] == "Completed":
            return action_response(
                False, "This report is already completed.", report, 400
            )

        image = request.files.get("clean-image")
        if not image or not image.filename:
            return action_response(
                False, "Please upload an after-cleaning photo.", report, 400
            )

        if not allowed_file(image.filename):
            return action_response(
                False,
                "Invalid image format. Use JPG, JPEG, PNG, GIF, or WEBP.",
                report, 400
            )

        original_filename = secure_filename(image.filename)
        if "." not in original_filename:
            return action_response(
                False, "Invalid image filename.", report, 400
            )

        extension = original_filename.rsplit(".", 1)[1].lower()
        filename = f"clean_{uuid.uuid4().hex}.{extension}"
        image.save(os.path.join(UPLOAD_FOLDER, filename))

        completed_at = datetime.now().strftime(DATE_FORMAT)
        db_execute("""
            UPDATE reports
            SET clean_image = %s, status = %s, completed_at = %s
            WHERE report_code = %s
        """, (filename, "Completed", completed_at, report_id))

        report["clean_image"] = filename
        report["status"] = "Completed"
        report["completed_at"] = completed_at

        create_notification(
            "Report Completed",
            f"Report {report_id} has been successfully completed.",
            "completed",
            report_id
        )
        return action_response(
            True, "Report completed successfully!", report
        )
    except Exception:
        app.logger.exception("Complete report error")
        return action_response(
            False, "Could not complete report. Check the database.",
            status_code=500
        )


# ==================================================
# RESIDENT SIGNUP
# ==================================================

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        fullname = request.form.get("fullname", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not fullname or not email or not password:
            flash("Please complete all required fields.")
            return render_template("signup.html")

        if password != confirm_password:
            flash("Passwords do not match.")
            return render_template("signup.html")

        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return render_template("signup.html")

        try:
            existing = db_fetchone(
                "SELECT id FROM users WHERE email = %s LIMIT 1",
                (email,)
            )
            if existing:
                flash("Email is already registered.")
                return render_template("signup.html")

            db_execute("""
                INSERT INTO users (fullname, email, password)
                VALUES (%s, %s, %s)
            """, (
                fullname,
                email,
                generate_password_hash(password)
            ))

            flash("Account created successfully! You can now log in.")
            return redirect(url_for("resident_login"))

        except (pymysql.err.IntegrityError if pymysql else ()):
            flash("Email is already registered.")
        except Exception:
            app.logger.exception("Signup database error")
            flash("Could not create account. Check database settings.")

    return render_template("signup.html")


# ==================================================
# RESIDENT LOGIN
# ==================================================

@app.route("/resident-login", methods=["GET", "POST"])
def resident_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        try:
            resident = db_fetchone("""
                SELECT id, fullname, email, password
                FROM users
                WHERE email = %s
                LIMIT 1
            """, (email,))
        except Exception:
            app.logger.exception("Resident login database error")
            flash("Database connection failed. Please try again.")
            return render_template("resident_login.html")

        if resident and check_password_hash(resident["password"], password):
            session.clear()
            session["resident_id"] = resident["id"]
            session["resident_email"] = resident["email"]
            session["resident_name"] = resident["fullname"]
            return redirect(url_for("resident_home"))

        flash("Invalid email or password. Please create an account first.")

    return render_template("resident_login.html")


# ==================================================
# RESIDENT DASHBOARD
# ==================================================

@app.route("/resident-home")
def resident_home():
    if "resident_email" not in session:
        return redirect(url_for("resident_login"))

    try:
        reports = [
            prepare_report(report)
            for report in get_all_reports()
            if report["email"] == session["resident_email"]
        ]
    except Exception:
        app.logger.exception("Resident dashboard database error")
        flash("Could not load your reports.")
        reports = []

    return render_template(
        "resident_home.html",
        reports=reports,
        resident_name=session.get("resident_name", "")
    )


@app.route("/api/resident/reports")
def resident_reports_api():
    if "resident_email" not in session:
        return jsonify({
            "success": False,
            "message": "Please log in first."
        }), 401

    try:
        reports = [
            prepare_report(r)
            for r in get_all_reports()
            if r["email"] == session["resident_email"]
        ]
        return jsonify({
            "success": True,
            "reports": reports,
            "total_reports": len(reports),
            "pending_reports": sum(r["status"] == "Pending" for r in reports),
            "in_progress_reports": sum(
                r["status"] == "In Progress" for r in reports
            ),
            "completed_reports": sum(
                r["status"] == "Completed" for r in reports
            )
        })
    except Exception:
        app.logger.exception("Resident reports API error")
        return jsonify({
            "success": False,
            "message": "Could not load reports."
        }), 500


# ==================================================
# SUBMIT GARBAGE REPORT
# ==================================================

@app.route("/submit-report", methods=["POST"])
def submit_report():
    if "resident_email" not in session:
        if is_ajax_request():
            return jsonify({
                "success": False,
                "message": "Your session has expired. Please log in again."
            }), 401
        return redirect(url_for("resident_login"))

    fullname = request.form.get("fullname", "").strip()
    report_date = request.form.get("date", "").strip()
    report_time = request.form.get("time", "").strip()
    sitio = request.form.get("sitio", "").strip()
    description = request.form.get("description", "").strip()
    priority = request.form.get("priority", "Normal").strip()
    image = request.files.get("garbage-image")

    if not fullname or not report_date or not report_time or not sitio:
        return report_error("Please complete all required report fields.")

    if priority not in ["Normal", "Urgent"]:
        priority = "Normal"

    if not image or not image.filename:
        return report_error("Please upload a picture of the garbage.")

    if not allowed_file(image.filename):
        return report_error(
            "Invalid image format. Use JPG, JPEG, PNG, GIF, or WEBP."
        )

    original_filename = secure_filename(image.filename)
    if "." not in original_filename:
        return report_error("Invalid image filename.")

    extension = original_filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{extension}"
    image_path = os.path.join(UPLOAD_FOLDER, filename)

    resident_email = session["resident_email"]
    now = datetime.now()

    try:
        latest = db_fetchone("""
            SELECT created_at
            FROM reports
            WHERE resident_email = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, (resident_email,))

        if latest:
            elapsed = (now - parse_datetime(latest["created_at"])).total_seconds()
            if elapsed < REPORT_COOLDOWN_SECONDS:
                seconds_left = int(REPORT_COOLDOWN_SECONDS - elapsed)
                minutes_left = (seconds_left + 59) // 60
                return report_error(
                    f"Please wait about {minutes_left} minute(s) "
                    "before submitting another report."
                )

        existing_reports = db_fetchall("""
            SELECT report_code, sitio, description
            FROM reports
            WHERE status <> 'Completed'
        """)

        normalized_sitio = " ".join(sitio.casefold().split())
        normalized_description = " ".join(description.casefold().split())

        if normalized_description:
            for existing in existing_reports:
                old_sitio = " ".join(
                    (existing.get("sitio") or "").casefold().split()
                )
                old_description = " ".join(
                    (existing.get("description") or "").casefold().split()
                )

                if (
                    old_sitio == normalized_sitio
                    and old_description == normalized_description
                ):
                    return report_error(
                        "Possible duplicate report detected "
                        f"(Report ID: {existing['report_code']}). "
                        "Please check the existing report first."
                    )

        image.save(image_path)

        report_code = (
            f"BR-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8].upper()}"
        )
        created_at = now.strftime(DATE_FORMAT)

        db_execute("""
            INSERT INTO reports (
                report_code, resident_email, fullname, report_date,
                report_time, sitio, description, image, clean_image,
                priority, status, created_at, completed_at
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, NULL,
                %s, 'Pending', %s, NULL
            )
        """, (
            report_code, resident_email, fullname, report_date,
            report_time, sitio, description, filename,
            priority, created_at
        ))

        if priority == "Urgent":
            create_notification(
                "Urgent Garbage Report",
                f"{fullname} submitted an URGENT report at {sitio}.",
                "urgent",
                report_code
            )
        else:
            create_notification(
                "New Garbage Report",
                f"{fullname} submitted a new report at {sitio}.",
                "new_report",
                report_code
            )

    except Exception:
        app.logger.exception("Report submission database error")
        try:
            if os.path.exists(image_path):
                os.remove(image_path)
        except OSError:
            pass
        return report_error(
            "Could not save your report. Please try again.", 500
        )

    new_report = {
        "id": report_code,
        "email": resident_email,
        "fullname": fullname,
        "date": report_date,
        "time": report_time,
        "sitio": sitio,
        "description": description,
        "image": filename,
        "clean_image": None,
        "priority": priority,
        "status": "Pending",
        "created_at": created_at,
        "completed_at": None
    }

    if is_ajax_request():
        return jsonify({
            "success": True,
            "message": "Garbage report submitted successfully!",
            "report": prepare_report(new_report)
        })

    flash("Garbage report submitted successfully!")
    return redirect(url_for("resident_home"))


# ==================================================
# DISPLAY UPLOADED IMAGES
# ==================================================

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(app.config["UPLOAD_FOLDER"], filename)


# ==================================================
# LOGOUT
# ==================================================

@app.route("/logout")
def logout():
    session.clear()
    flash("You have logged out successfully.")
    return redirect(url_for("login"))


# ==================================================
# TEST DATABASE
# ==================================================

@app.route("/test-db")
def test_db():
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS db_name")
            result = cursor.fetchone()

        return jsonify({
            "success": True,
            "message": "Connected to MySQL successfully!",
            "database": result["db_name"]
        })
    except Exception:
        app.logger.exception("Database connection test failed")
        return jsonify({
            "success": False,
            "message": "Database connection failed. Check terminal logs."
        }), 500
    finally:
        if connection:
            connection.close()


# ==================================================
# RUN APP
# ==================================================

if __name__ == "__main__":
    init_db()
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "1") == "1"
    )
