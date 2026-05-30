import base64
import csv
import io
import json
import os
import shutil
import socket
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urljoin, urlparse
from urllib.request import Request, urlopen

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "event_id_system.db")
ACTIVE_DB_FILE = os.path.join(BASE_DIR, "active_database.json")
SETTINGS_FILE = os.path.join(BASE_DIR, "event_settings.json")
PHOTO_DIR = os.path.join(BASE_DIR, "photos")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
PHOTO_URL_PREFIX = "photos"
HOST = "0.0.0.0"
PORT = 8000
AUTO_BACKUP_CHECK_SECONDS = 60
AUTO_BACKUP_DEFAULT_MINUTES = 15
backup_lock = threading.Lock()
auto_backup_stop_event = threading.Event()

CATEGORIES = ("Delegate", "Officer", "Speaker", "Lecturer", "Pastor", "Pastor's Wife", "Children", "Facilitator", "Staff")
PRINT_STATUSES = ("Printing", "Done", "Cancel", "Error")
MEAL_PLAN_OPTIONS = (
    "May 26 - Dinner",
    "May 27 - Breakfast",
    "May 27 - Lunch",
    "May 27 - Dinner",
    "May 28 - Breakfast",
    "May 28 - Lunch",
    "May 28 - Dinner",
    "May 29 - Breakfast",
)
CHURCHES_BY_DISTRICT = {
    "District 1": (
        "Tagbilaran Community Christian Church",
        "Central Christian Church",
        "Albuquerque Christian Church",
        "Cortes Christian Church",
        "Panglao Island Church of Christ",
    ),
    "District 2": (
        "Abijilan Christian Church",
        "Datag Christian Church",
        "Jagna Church of Christ",
        "Mayana Church of Christ",
        "Duero Church of Christ",
        "Cabantian Church of Christ",
        "Guindulman Church of Christ",
    ),
    "District 3": (
        "Chocolate Hills Christian Church",
        "Salvador Church of Christ",
        "Canlangit Church of Christ",
        "Babag Heir Christian Church",
        "Mountain View Church of Christ",
    ),
    "District 4": (
        "Calape Church of Christ",
        "Faith Christian Church",
        "Tubigon Church of Christ",
        "Twin Hills Church of Christ",
        "Getafe Church of Christ",
        "Loon Church of Christ",
    ),
    "District 5": (
        "Gabi Church of Jesus Christ Ministry",
        "Cambangay Norte Church of Christ",
        "Balintawak Church of Christ - Talibon",
        "Bohol Island Church of Christ",
        "Katarungan Church of Christ",
        "Cabaasan Church of Christ",
        "Mabojoc Church of Christ",
    ),
}


def connect_db():
    conn = sqlite3.connect(get_active_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def get_active_db_path():
    if os.path.exists(ACTIVE_DB_FILE):
        try:
            with open(ACTIVE_DB_FILE, "r", encoding="utf-8") as state_file:
                filename = json.load(state_file).get("filename", "")
            candidate = os.path.abspath(os.path.join(BASE_DIR, filename))

            if candidate.startswith(BASE_DIR) and candidate.lower().endswith(".db"):
                return candidate
        except Exception:
            pass

    return DB_NAME


def set_active_db(filename):
    candidate = os.path.abspath(os.path.join(BASE_DIR, filename))

    if not candidate.startswith(BASE_DIR) or not candidate.lower().endswith(".db"):
        return None

    with open(ACTIVE_DB_FILE, "w", encoding="utf-8") as state_file:
        json.dump({"filename": os.path.basename(candidate)}, state_file)

    return candidate


def ensure_database():
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = connect_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS churches (
        church_id INTEGER PRIMARY KEY AUTOINCREMENT,
        church_name TEXT NOT NULL UNIQUE
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS participants (
        participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER,
        church_id INTEGER,
        id_number TEXT NOT NULL UNIQUE,
        full_name TEXT NOT NULL,
        age INTEGER,
        gender TEXT CHECK(gender IN ('Male', 'Female')),
        address TEXT,
        contact_number TEXT,
        category TEXT CHECK(category IN ('Delegate', 'Officer', 'Speaker', 'Lecturer', 'Pastor', 'Pastor''s Wife', 'Children', 'Facilitator', 'Staff')) DEFAULT 'Delegate',
        lecture TEXT,
        team_index INTEGER,
        team_name TEXT,
        team_color TEXT,
        photo_path TEXT,
        id_status TEXT CHECK(id_status IN ('Not Printed', 'Printed')) DEFAULT 'Not Printed',
        date_registered DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (church_id) REFERENCES churches(church_id)
    )
    """)

    cursor.execute("PRAGMA table_info(participants)")
    columns = {column["name"] for column in cursor.fetchall()}

    if "district" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN district TEXT")
        columns.add("district")

    if "check_in_status" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN check_in_status TEXT DEFAULT 'Not Yet'")
        columns.add("check_in_status")

    if "checked_in_at" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN checked_in_at DATETIME")
        columns.add("checked_in_at")

    if "payment_status" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN payment_status TEXT DEFAULT 'Unpaid'")
        cursor.execute("UPDATE participants SET payment_status = 'Paid' WHERE check_in_status = 'Paid Registration'")
        cursor.execute("UPDATE participants SET check_in_status = 'Not Yet' WHERE check_in_status = 'Paid Registration'")
        columns.add("payment_status")

    if "payment_received_at" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN payment_received_at DATETIME")
        columns.add("payment_received_at")

    if "payment_amount" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN payment_amount REAL DEFAULT 0")
        columns.add("payment_amount")

    if "payment_reference" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN payment_reference TEXT")
        columns.add("payment_reference")

    if "payment_note" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN payment_note TEXT")
        columns.add("payment_note")

    cursor.execute("UPDATE participants SET checked_in_at = NULL WHERE payment_status = 'Paid' AND check_in_status = 'Not Yet'")

    if "team_index" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN team_index INTEGER")
        columns.add("team_index")

    if "team_name" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN team_name TEXT")
        columns.add("team_name")

    if "team_color" not in columns:
        cursor.execute("ALTER TABLE participants ADD COLUMN team_color TEXT")
        columns.add("team_color")

    migrate_participants_table(cursor, columns)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS print_history (
        history_id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_number TEXT,
        participant_name TEXT,
        category TEXT,
        district TEXT,
        church_name TEXT,
        lecture TEXT,
        print_mode TEXT,
        batch_size INTEGER,
        status TEXT DEFAULT 'Done',
        printed_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("PRAGMA table_info(print_history)")
    history_columns = {column["name"] for column in cursor.fetchall()}

    if "status" not in history_columns:
        cursor.execute("ALTER TABLE print_history ADD COLUMN status TEXT DEFAULT 'Done'")

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS meal_stub_registrations (
        meal_stub_id INTEGER PRIMARY KEY AUTOINCREMENT,
        church_id INTEGER,
        full_name TEXT NOT NULL,
        age INTEGER,
        gender TEXT CHECK(gender IN ('Male', 'Female')),
        contact_number TEXT,
        district TEXT,
        category TEXT CHECK(category IN ('Delegate', 'Officer', 'Speaker', 'Lecturer', 'Pastor', 'Pastor''s Wife', 'Children', 'Facilitator', 'Staff')) DEFAULT 'Delegate',
        meal_plan TEXT,
        id_jacket_sling INTEGER DEFAULT 0,
        claimed_meals TEXT DEFAULT '[]',
        date_registered DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (church_id) REFERENCES churches(church_id)
    )
    """)

    cursor.execute("PRAGMA table_info(meal_stub_registrations)")
    meal_stub_columns = {column["name"] for column in cursor.fetchall()}

    if "claimed_meals" not in meal_stub_columns:
        cursor.execute("ALTER TABLE meal_stub_registrations ADD COLUMN claimed_meals TEXT DEFAULT '[]'")

    if "id_jacket_sling" not in meal_stub_columns:
        cursor.execute("ALTER TABLE meal_stub_registrations ADD COLUMN id_jacket_sling INTEGER DEFAULT 0")

    migrate_meal_stub_registrations_table(cursor, meal_stub_columns)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS meal_claim_log (
        claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
        meal_stub_id INTEGER,
        meal_name TEXT NOT NULL,
        action TEXT CHECK(action IN ('Claimed', 'Unclaimed')) NOT NULL,
        claimed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (meal_stub_id) REFERENCES meal_stub_registrations(meal_stub_id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS activity_log (
        activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        details TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS payment_transactions (
        transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        id_number TEXT,
        participant_name TEXT,
        church_name TEXT,
        district TEXT,
        category TEXT,
        action TEXT CHECK(action IN ('Paid', 'Unpaid')) NOT NULL,
        amount REAL DEFAULT 0,
        reference TEXT,
        note TEXT,
        updated_by TEXT,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    conn.commit()
    conn.close()


def migrate_participants_table(cursor, columns):
    cursor.execute("""
    SELECT sql
    FROM sqlite_master
    WHERE type = 'table' AND name = 'participants'
    """)
    table_sql = cursor.fetchone()["sql"]

    if "Staff" in table_sql:
        return

    cursor.execute("ALTER TABLE participants RENAME TO participants_old")

    cursor.execute("""
    CREATE TABLE participants (
        participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER,
        church_id INTEGER,
        id_number TEXT NOT NULL UNIQUE,
        full_name TEXT NOT NULL,
        age INTEGER,
        gender TEXT CHECK(gender IN ('Male', 'Female')),
        address TEXT,
        contact_number TEXT,
        category TEXT CHECK(category IN ('Delegate', 'Officer', 'Speaker', 'Lecturer', 'Pastor', 'Pastor''s Wife', 'Children', 'Facilitator', 'Staff')) DEFAULT 'Delegate',
        lecture TEXT,
        team_index INTEGER,
        team_name TEXT,
        team_color TEXT,
        photo_path TEXT,
        id_status TEXT CHECK(id_status IN ('Not Printed', 'Printed')) DEFAULT 'Not Printed',
        date_registered DATETIME DEFAULT CURRENT_TIMESTAMP,
        district TEXT,
        check_in_status TEXT DEFAULT 'Not Yet',
        checked_in_at DATETIME,
        payment_status TEXT DEFAULT 'Unpaid',
        payment_received_at DATETIME,
        payment_amount REAL DEFAULT 0,
        payment_reference TEXT,
        payment_note TEXT,
        FOREIGN KEY (church_id) REFERENCES churches(church_id)
    )
    """)

    district_value = "district" if "district" in columns else "''"
    check_in_status_value = "check_in_status" if "check_in_status" in columns else "'Not Yet'"
    checked_in_at_value = "checked_in_at" if "checked_in_at" in columns else "NULL"
    team_index_value = "team_index" if "team_index" in columns else "NULL"
    team_name_value = "team_name" if "team_name" in columns else "''"
    team_color_value = "team_color" if "team_color" in columns else "''"
    payment_status_value = "payment_status" if "payment_status" in columns else "CASE WHEN check_in_status = 'Paid Registration' THEN 'Paid' ELSE 'Unpaid' END"
    payment_received_at_value = "payment_received_at" if "payment_received_at" in columns else "NULL"
    payment_amount_value = "payment_amount" if "payment_amount" in columns else "0"
    payment_reference_value = "payment_reference" if "payment_reference" in columns else "''"
    payment_note_value = "payment_note" if "payment_note" in columns else "''"

    cursor.execute(f"""
    INSERT INTO participants (
        participant_id, event_id, church_id, id_number, full_name, age, gender,
        address, contact_number, category, lecture, photo_path, id_status,
        date_registered, district, check_in_status, checked_in_at,
        team_index, team_name, team_color, payment_status, payment_received_at,
        payment_amount, payment_reference, payment_note
    )
    SELECT
        participant_id, event_id, church_id, id_number, full_name, age, gender,
        address, contact_number,
        CASE
            WHEN category IN ('Delegate', 'Officer', 'Speaker', 'Lecturer', 'Pastor', 'Pastor''s Wife', 'Children', 'Facilitator', 'Staff')
            THEN category
            ELSE 'Delegate'
        END,
        lecture, photo_path, id_status, date_registered, {district_value},
        CASE WHEN {check_in_status_value} = 'Paid Registration' THEN 'Not Yet' ELSE {check_in_status_value} END,
        {checked_in_at_value},
        {team_index_value}, {team_name_value}, {team_color_value}, {payment_status_value}, {payment_received_at_value},
        {payment_amount_value}, {payment_reference_value}, {payment_note_value}
    FROM participants_old
    """)

    cursor.execute("DROP TABLE participants_old")


def migrate_meal_stub_registrations_table(cursor, columns):
    cursor.execute("""
    SELECT sql
    FROM sqlite_master
    WHERE type = 'table' AND name = 'meal_stub_registrations'
    """)
    table_sql = cursor.fetchone()["sql"]

    if "Staff" in table_sql:
        return

    cursor.execute("ALTER TABLE meal_stub_registrations RENAME TO meal_stub_registrations_old")

    cursor.execute("""
    CREATE TABLE meal_stub_registrations (
        meal_stub_id INTEGER PRIMARY KEY AUTOINCREMENT,
        church_id INTEGER,
        full_name TEXT NOT NULL,
        age INTEGER,
        gender TEXT CHECK(gender IN ('Male', 'Female')),
        contact_number TEXT,
        district TEXT,
        category TEXT CHECK(category IN ('Delegate', 'Officer', 'Speaker', 'Lecturer', 'Pastor', 'Pastor''s Wife', 'Children', 'Facilitator', 'Staff')) DEFAULT 'Delegate',
        meal_plan TEXT,
        id_jacket_sling INTEGER DEFAULT 0,
        claimed_meals TEXT DEFAULT '[]',
        date_registered DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (church_id) REFERENCES churches(church_id)
    )
    """)

    id_jacket_sling_value = "id_jacket_sling" if "id_jacket_sling" in columns else "0"
    claimed_meals_value = "claimed_meals" if "claimed_meals" in columns else "'[]'"

    cursor.execute(f"""
    INSERT INTO meal_stub_registrations (
        meal_stub_id, church_id, full_name, age, gender, contact_number,
        district, category, meal_plan, id_jacket_sling, claimed_meals,
        date_registered
    )
    SELECT
        meal_stub_id, church_id, full_name, age, gender, contact_number,
        district,
        CASE
            WHEN category IN ('Delegate', 'Officer', 'Speaker', 'Lecturer', 'Pastor', 'Pastor''s Wife', 'Children', 'Facilitator', 'Staff')
            THEN category
            ELSE 'Delegate'
        END,
        meal_plan, {id_jacket_sling_value}, {claimed_meals_value},
        date_registered
    FROM meal_stub_registrations_old
    """)

    cursor.execute("DROP TABLE meal_stub_registrations_old")


def get_or_create_church_id(cursor, church_name):
    cursor.execute("INSERT OR IGNORE INTO churches (church_name) VALUES (?)", (church_name,))
    cursor.execute("SELECT church_id FROM churches WHERE church_name = ?", (church_name,))
    return cursor.fetchone()["church_id"]


def next_id_number(cursor):
    cursor.execute("SELECT COALESCE(MAX(participant_id), 0) + 1 AS next_id FROM participants")
    return f"RT-{cursor.fetchone()['next_id']:04d}"


def save_photo(photo_data, existing_path=""):
    if not photo_data:
        return existing_path or ""

    if "," in photo_data:
        header, encoded = photo_data.split(",", 1)
    else:
        header, encoded = "", photo_data

    extension = "png"
    if "image/jpeg" in header or "image/jpg" in header:
        extension = "jpg"
    elif "image/webp" in header:
        extension = "webp"

    filename = f"participant-{int(time.time() * 1000)}.{extension}"
    path = os.path.join(PHOTO_DIR, filename)

    with open(path, "wb") as photo_file:
        photo_file.write(base64.b64decode(encoded))

    return f"{PHOTO_URL_PREFIX}/{filename}"


def build_ip_webcam_snapshot_url(camera_url):
    parsed = urlparse((camera_url or "").strip())

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""

    path = parsed.path.rstrip("/")
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    if path.endswith("/shot.jpg") or path.endswith("/photo.jpg"):
        return camera_url.strip()

    if path.endswith("/video"):
        return urljoin(base_url, "/shot.jpg")

    if not path:
        return urljoin(base_url, "/shot.jpg")

    return urljoin(base_url, "/shot.jpg")


def capture_ip_webcam_snapshot(camera_url):
    snapshot_url = build_ip_webcam_snapshot_url(camera_url)

    if not snapshot_url:
        return None

    request = Request(snapshot_url, headers={"User-Agent": "EventIDSystem/1.0"})

    with urlopen(request, timeout=8) as response:
        content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
        image_bytes = response.read(8 * 1024 * 1024)

    if not content_type.startswith("image/") or not image_bytes:
        return None

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return {
        "photoData": f"data:{content_type};base64,{encoded}",
        "snapshotUrl": snapshot_url,
    }


def participant_from_row(row):
    church_name = row["church_name"] or ""
    district = row["district"] or infer_district(church_name)
    photo_path = (row["photo_path"] or "").replace("\\", "/")

    return {
        "id": row["id_number"],
        "name": row["full_name"],
        "age": "" if row["age"] is None else str(row["age"]),
        "gender": row["gender"] or "Male",
        "contactNumber": row["contact_number"] or "",
        "district": district,
        "church": church_name,
        "category": row["category"] or "Delegate",
        "lecture": row["lecture"] or "",
        "teamIndex": row["team_index"],
        "teamName": row["team_name"] or "",
        "teamColor": row["team_color"] or "",
        "photoPreview": f"http://{HOST}:{PORT}/{photo_path}" if photo_path else None,
        "checkInStatus": row["check_in_status"] or "Not Yet",
        "checkedInAt": row["checked_in_at"] or "",
        "paymentStatus": row["payment_status"] or "Unpaid",
        "paymentReceivedAt": row["payment_received_at"] or "",
        "paymentAmount": float(row["payment_amount"] or 0) if "payment_amount" in row.keys() else 0,
        "paymentReference": (row["payment_reference"] or "") if "payment_reference" in row.keys() else "",
        "paymentNote": (row["payment_note"] or "") if "payment_note" in row.keys() else "",
    }


def fetch_participant_by_id(cursor, id_number):
    cursor.execute("""
    SELECT participants.*, churches.church_name
    FROM participants
    LEFT JOIN churches ON participants.church_id = churches.church_id
    WHERE participants.id_number = ?
    """, (id_number,))
    row = cursor.fetchone()
    return participant_from_row(row) if row else None


def infer_district(church_name):
    for district, churches in CHURCHES_BY_DISTRICT.items():
        if church_name in churches:
            return district

    return ""


def fetch_participants():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT participants.*, churches.church_name
    FROM participants
    LEFT JOIN churches ON participants.church_id = churches.church_id
    ORDER BY participants.participant_id DESC
    """)
    participants = [participant_from_row(row) for row in cursor.fetchall()]
    conn.close()
    return participants


def create_participant(data):
    conn = connect_db()
    cursor = conn.cursor()
    church_id = get_or_create_church_id(cursor, data["church"])
    id_number = next_id_number(cursor)
    photo_path = save_photo(data.get("photoData"))

    cursor.execute("""
    INSERT INTO participants (
        event_id, church_id, id_number, full_name, age, gender, address,
        contact_number, category, lecture, district, team_index, team_name,
        team_color, photo_path
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        1,
        church_id,
        id_number,
        data["name"],
        int(data["age"]) if str(data.get("age", "")).isdigit() else None,
        data.get("gender", "Male"),
        "",
        data.get("contactNumber", ""),
        data.get("category", "Delegate") if data.get("category") in CATEGORIES else "Delegate",
        data.get("lecture", ""),
        data.get("district", ""),
        data.get("teamIndex"),
        data.get("teamName", ""),
        data.get("teamColor", ""),
        photo_path,
    ))

    conn.commit()
    cursor.execute("""
    SELECT participants.*, churches.church_name
    FROM participants
    LEFT JOIN churches ON participants.church_id = churches.church_id
    WHERE participants.id_number = ?
    """, (id_number,))
    participant = participant_from_row(cursor.fetchone())
    conn.close()
    log_activity("Participant Created", "participant", id_number, data.get("name", ""))
    return participant


def update_participant(id_number, data):
    conn = connect_db()
    cursor = conn.cursor()
    church_id = get_or_create_church_id(cursor, data["church"])

    cursor.execute("SELECT photo_path FROM participants WHERE id_number = ?", (id_number,))
    existing = cursor.fetchone()
    if not existing:
        conn.close()
        return None

    photo_path = save_photo(data.get("photoData"), existing["photo_path"] or "")

    cursor.execute("""
    UPDATE participants
    SET church_id = ?, full_name = ?, age = ?, gender = ?, contact_number = ?,
        category = ?, lecture = ?, district = ?, team_index = ?, team_name = ?,
        team_color = ?, photo_path = ?
    WHERE id_number = ?
    """, (
        church_id,
        data["name"],
        int(data["age"]) if str(data.get("age", "")).isdigit() else None,
        data.get("gender", "Male"),
        data.get("contactNumber", ""),
        data.get("category", "Delegate") if data.get("category") in CATEGORIES else "Delegate",
        data.get("lecture", ""),
        data.get("district", ""),
        data.get("teamIndex"),
        data.get("teamName", ""),
        data.get("teamColor", ""),
        photo_path,
        id_number,
    ))

    conn.commit()
    cursor.execute("""
    SELECT participants.*, churches.church_name
    FROM participants
    LEFT JOIN churches ON participants.church_id = churches.church_id
    WHERE participants.id_number = ?
    """, (id_number,))
    participant = participant_from_row(cursor.fetchone())
    conn.close()
    log_activity("Participant Updated", "participant", id_number, data.get("name", ""))
    return participant


def update_participant_check_in(id_number, status):
    if status not in ("Arrived", "Not Yet", "Cancelled"):
        status = "Not Yet"

    checked_in_at = time.strftime("%Y-%m-%d %H:%M:%S") if status == "Arrived" else None
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("""
    UPDATE participants
    SET check_in_status = ?, checked_in_at = ?
    WHERE id_number = ?
    """, (status, checked_in_at, id_number))

    if cursor.rowcount == 0:
        conn.close()
        return None

    conn.commit()
    participant = fetch_participant_by_id(cursor, id_number)
    conn.close()
    log_activity("Check-in Updated", "participant", id_number, status)
    return participant


def update_participant_payment(id_number, data):
    status = data.get("status", "Unpaid") if isinstance(data, dict) else data
    if status not in ("Paid", "Unpaid"):
        status = "Unpaid"

    payment_received_at = time.strftime("%Y-%m-%d %H:%M:%S") if status == "Paid" else None
    amount = data.get("amount", 0) if isinstance(data, dict) else 0
    reference = data.get("reference", "") if isinstance(data, dict) else ""
    note = data.get("note", "") if isinstance(data, dict) else ""
    updated_by = data.get("updatedBy", "Admin") if isinstance(data, dict) else "Admin"

    try:
        amount = float(amount or 0)
    except (TypeError, ValueError):
        amount = 0

    conn = connect_db()
    cursor = conn.cursor()
    existing = fetch_participant_by_id(cursor, id_number)

    if existing is None:
        conn.close()
        return None

    if status == "Unpaid":
        amount = 0
        reference = ""

    cursor.execute("""
    UPDATE participants
    SET payment_status = ?, payment_received_at = ?, payment_amount = ?, payment_reference = ?, payment_note = ?
    WHERE id_number = ?
    """, (status, payment_received_at, amount, reference, note, id_number))

    if cursor.rowcount == 0:
        conn.close()
        return None

    cursor.execute("""
    INSERT INTO payment_transactions (
        id_number, participant_name, church_name, district, category,
        action, amount, reference, note, updated_by
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        id_number,
        existing["name"],
        existing["church"],
        existing["district"],
        existing["category"],
        status,
        amount,
        reference,
        note,
        updated_by,
    ))

    conn.commit()
    participant = fetch_participant_by_id(cursor, id_number)
    conn.close()
    log_activity("Payment Updated", "participant", id_number, f"{status} {amount:.2f}".strip())
    return participant


def update_bulk_participant_check_in(data):
    ids = data.get("ids", [])
    status = data.get("status", "Not Yet")
    updated = []

    for id_number in ids:
        participant = update_participant_check_in(id_number, status)
        if participant:
            updated.append(participant)

    return updated


def update_bulk_participant_payment(data):
    ids = data.get("ids", [])
    status = data.get("status", "Unpaid")
    updated = []

    for id_number in ids:
        participant = update_participant_payment(id_number, { **data, "status": status })
        if participant:
            updated.append(participant)

    return updated


def meal_stub_from_row(row):
    church_name = row["church_name"] or ""
    meal_plan = []
    claimed_meals = []

    try:
        meal_plan = json.loads(row["meal_plan"] or "[]")
    except json.JSONDecodeError:
        meal_plan = []

    try:
        claimed_meals = json.loads(row["claimed_meals"] or "[]")
    except (KeyError, json.JSONDecodeError):
        claimed_meals = []

    return {
        "id": row["meal_stub_id"],
        "name": row["full_name"],
        "age": "" if row["age"] is None else str(row["age"]),
        "gender": row["gender"] or "Male",
        "contactNumber": row["contact_number"] or "",
        "district": row["district"] or infer_district(church_name),
        "church": church_name,
        "category": row["category"] or "Delegate",
        "mealPlan": meal_plan,
        "idJacketSling": bool(row["id_jacket_sling"]) if "id_jacket_sling" in row.keys() else False,
        "claimedMeals": claimed_meals,
        "dateRegistered": row["date_registered"],
    }


def fetch_meal_stubs():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT meal_stub_registrations.*, churches.church_name
    FROM meal_stub_registrations
    LEFT JOIN churches ON meal_stub_registrations.church_id = churches.church_id
    ORDER BY meal_stub_registrations.meal_stub_id DESC
    """)
    meal_stubs = [meal_stub_from_row(row) for row in cursor.fetchall()]
    conn.close()
    return meal_stubs


def create_meal_stub(data):
    conn = connect_db()
    cursor = conn.cursor()
    church_id = get_or_create_church_id(cursor, data["church"])
    selected_meals = [
        meal for meal in data.get("mealPlan", [])
        if meal in MEAL_PLAN_OPTIONS
    ]

    cursor.execute("""
    INSERT INTO meal_stub_registrations (
        church_id, full_name, age, gender, contact_number, district, category, meal_plan, id_jacket_sling
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        church_id,
        data["name"],
        int(data["age"]) if str(data.get("age", "")).isdigit() else None,
        data.get("gender", "Male"),
        data.get("contactNumber", ""),
        data.get("district", ""),
        data.get("category", "Delegate") if data.get("category") in CATEGORIES else "Delegate",
        json.dumps(selected_meals),
        1 if data.get("idJacketSling") else 0,
    ))

    meal_stub_id = cursor.lastrowid
    conn.commit()
    cursor.execute("""
    SELECT meal_stub_registrations.*, churches.church_name
    FROM meal_stub_registrations
    LEFT JOIN churches ON meal_stub_registrations.church_id = churches.church_id
    WHERE meal_stub_registrations.meal_stub_id = ?
    """, (meal_stub_id,))
    meal_stub = meal_stub_from_row(cursor.fetchone())
    conn.close()
    return meal_stub


def update_meal_stub(meal_stub_id, data):
    conn = connect_db()
    cursor = conn.cursor()
    church_id = get_or_create_church_id(cursor, data["church"])
    selected_meals = [
        meal for meal in data.get("mealPlan", [])
        if meal in MEAL_PLAN_OPTIONS
    ]
    claimed_meals = [
        meal for meal in data.get("claimedMeals", [])
        if meal in selected_meals
    ]

    cursor.execute("SELECT meal_stub_id FROM meal_stub_registrations WHERE meal_stub_id = ?", (meal_stub_id,))
    existing = cursor.fetchone()
    if not existing:
        conn.close()
        return None

    cursor.execute("""
    UPDATE meal_stub_registrations
    SET church_id = ?, full_name = ?, age = ?, gender = ?, contact_number = ?,
        district = ?, category = ?, meal_plan = ?, id_jacket_sling = ?, claimed_meals = ?
    WHERE meal_stub_id = ?
    """, (
        church_id,
        data["name"],
        int(data["age"]) if str(data.get("age", "")).isdigit() else None,
        data.get("gender", "Male"),
        data.get("contactNumber", ""),
        data.get("district", ""),
        data.get("category", "Delegate") if data.get("category") in CATEGORIES else "Delegate",
        json.dumps(selected_meals),
        1 if data.get("idJacketSling") else 0,
        json.dumps(claimed_meals),
        meal_stub_id,
    ))

    conn.commit()
    cursor.execute("""
    SELECT meal_stub_registrations.*, churches.church_name
    FROM meal_stub_registrations
    LEFT JOIN churches ON meal_stub_registrations.church_id = churches.church_id
    WHERE meal_stub_registrations.meal_stub_id = ?
    """, (meal_stub_id,))
    meal_stub = meal_stub_from_row(cursor.fetchone())
    conn.close()
    return meal_stub


def update_meal_stub_claims(meal_stub_id, data):
    claimed_meals = [
        meal for meal in data.get("claimedMeals", [])
        if meal in MEAL_PLAN_OPTIONS
    ]

    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT meal_plan, claimed_meals FROM meal_stub_registrations WHERE meal_stub_id = ?", (meal_stub_id,))
    existing = cursor.fetchone()

    if not existing:
        conn.close()
        return None

    try:
        selected_meals = json.loads(existing["meal_plan"] or "[]")
    except json.JSONDecodeError:
        selected_meals = []

    valid_claims = [meal for meal in claimed_meals if meal in selected_meals]
    try:
        previous_claims = json.loads(existing["claimed_meals"] or "[]")
    except (KeyError, json.JSONDecodeError):
        previous_claims = []

    added_claims = [meal for meal in valid_claims if meal not in previous_claims]
    removed_claims = [meal for meal in previous_claims if meal not in valid_claims]
    cursor.execute(
        "UPDATE meal_stub_registrations SET claimed_meals = ? WHERE meal_stub_id = ?",
        (json.dumps(valid_claims), meal_stub_id),
    )

    for meal in added_claims:
        cursor.execute(
            "INSERT INTO meal_claim_log (meal_stub_id, meal_name, action) VALUES (?, ?, ?)",
            (meal_stub_id, meal, "Claimed"),
        )

    for meal in removed_claims:
        cursor.execute(
            "INSERT INTO meal_claim_log (meal_stub_id, meal_name, action) VALUES (?, ?, ?)",
            (meal_stub_id, meal, "Unclaimed"),
        )

    conn.commit()
    cursor.execute("""
    SELECT meal_stub_registrations.*, churches.church_name
    FROM meal_stub_registrations
    LEFT JOIN churches ON meal_stub_registrations.church_id = churches.church_id
    WHERE meal_stub_registrations.meal_stub_id = ?
    """, (meal_stub_id,))
    meal_stub = meal_stub_from_row(cursor.fetchone())
    conn.close()
    return meal_stub


def fetch_meal_claim_log():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT meal_claim_log.*, meal_stub_registrations.full_name, churches.church_name
    FROM meal_claim_log
    LEFT JOIN meal_stub_registrations ON meal_claim_log.meal_stub_id = meal_stub_registrations.meal_stub_id
    LEFT JOIN churches ON meal_stub_registrations.church_id = churches.church_id
    ORDER BY meal_claim_log.claim_id DESC
    LIMIT 300
    """)
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "claimId": row["claim_id"],
            "mealStubId": row["meal_stub_id"],
            "mealId": f"MS-{row['meal_stub_id']:04d}" if row["meal_stub_id"] else "",
            "name": row["full_name"] or "",
            "church": row["church_name"] or "",
            "meal": row["meal_name"],
            "action": row["action"],
            "claimedAt": row["claimed_at"],
        }
        for row in rows
    ]


def backup_database(reason="Manual backup"):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    backup_name = f"event_id_system-{timestamp}.db"
    backup_path = os.path.join(BACKUP_DIR, backup_name)

    with backup_lock:
        shutil.copy2(get_active_db_path(), backup_path)

    log_activity("Database Backup", "database", backup_name, reason)
    return {"backupFile": backup_name, "backupPath": backup_path}


def get_auto_backup_minutes(settings=None):
    if settings is None:
        settings = load_shared_settings()

    try:
        minutes = int(settings.get("autoBackupMinuteInterval", AUTO_BACKUP_DEFAULT_MINUTES))
    except (TypeError, ValueError):
        minutes = AUTO_BACKUP_DEFAULT_MINUTES

    return max(1, minutes)


def is_auto_backup_enabled(settings=None):
    if settings is None:
        settings = load_shared_settings()

    return settings.get("autoBackupEnabled", True) is not False


def get_latest_backup_path():
    if not os.path.isdir(BACKUP_DIR):
        return None

    backups = [
        os.path.join(BACKUP_DIR, filename)
        for filename in os.listdir(BACKUP_DIR)
        if filename.lower().endswith(".db")
    ]

    if not backups:
        return None

    return max(backups, key=os.path.getmtime)


def run_auto_backup_once():
    settings = load_shared_settings()

    if not is_auto_backup_enabled(settings):
        return None

    minutes = get_auto_backup_minutes(settings)
    latest_backup = get_latest_backup_path()
    now = time.time()

    if latest_backup and now - os.path.getmtime(latest_backup) < minutes * 60:
        return None

    return backup_database(f"Automatic backup after {minutes} minute interval")


def auto_backup_loop():
    while not auto_backup_stop_event.wait(AUTO_BACKUP_CHECK_SECONDS):
        try:
            run_auto_backup_once()
        except Exception as error:
            print(f"Automatic backup failed: {error}")


def start_auto_backup_worker():
    worker = threading.Thread(target=auto_backup_loop, daemon=True)
    worker.start()
    return worker


def log_activity(action, target_type="", target_id="", details=""):
    try:
      conn = connect_db()
      cursor = conn.cursor()
      cursor.execute("""
      INSERT INTO activity_log (action, target_type, target_id, details)
      VALUES (?, ?, ?, ?)
      """, (action, target_type, target_id, details))
      conn.commit()
      conn.close()
    except Exception:
      pass


def fetch_activity_log(limit=80):
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT *
    FROM activity_log
    ORDER BY activity_id DESC
    LIMIT ?
    """, (limit,))
    rows = [
        {
            "id": row["activity_id"],
            "action": row["action"],
            "targetType": row["target_type"] or "",
            "targetId": row["target_id"] or "",
            "details": row["details"] or "",
            "createdAt": row["created_at"] or "",
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    return rows


def restore_latest_backup():
    latest_backup = get_latest_backup_path()

    if latest_backup is None:
        return None

    safety_backup = backup_database("Safety backup before restore")["backupFile"]
    shutil.copy2(latest_backup, get_active_db_path())
    ensure_database()
    restored_name = os.path.basename(latest_backup)
    log_activity("Database Restore", "database", restored_name, f"Restored backup. Safety backup: {safety_backup}")
    return {"restoredFile": restored_name, "safetyBackupFile": safety_backup}


def list_databases():
    active_path = get_active_db_path()
    databases = []

    for filename in os.listdir(BASE_DIR):
        if not filename.lower().endswith(".db"):
            continue

        path = os.path.join(BASE_DIR, filename)
        if not os.path.isfile(path):
            continue

        databases.append({
            "filename": filename,
            "active": os.path.abspath(path) == os.path.abspath(active_path),
            "size": os.path.getsize(path),
            "modifiedAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
        })

    return sorted(databases, key=lambda item: (not item["active"], item["filename"].lower()))


def create_database():
    filename = f"event_id_system-{time.strftime('%Y%m%d-%H%M%S')}.db"
    path = set_active_db(filename)

    if path is None:
        return None

    ensure_database()
    log_activity("Database Created", "database", filename, "New active database created")
    return {"activeDatabase": filename, "databases": list_databases()}


def activate_database(filename):
    path = os.path.abspath(os.path.join(BASE_DIR, filename))

    if not path.startswith(BASE_DIR) or not os.path.isfile(path) or not path.lower().endswith(".db"):
        return None

    active_path = set_active_db(os.path.basename(path))

    if active_path is None:
        return None

    ensure_database()
    log_activity("Database Activated", "database", os.path.basename(path), "Active database switched")
    return {"activeDatabase": os.path.basename(path), "databases": list_databases()}


def make_csv_response(rows, headers):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def export_participants_csv():
    rows = [
        {
            "ID": participant["id"],
            "Full Name": participant["name"],
            "Age": participant["age"],
            "Gender": participant["gender"],
            "Contact Number": participant["contactNumber"],
            "District": participant["district"],
            "Church": participant["church"],
            "Category": participant["category"],
            "Lecture": participant["lecture"],
            "Team": participant["teamName"],
            "Team Color": participant["teamColor"],
            "Payment Status": participant["paymentStatus"],
            "Payment Amount": participant["paymentAmount"],
            "Payment Reference": participant["paymentReference"],
            "Payment Note": participant["paymentNote"],
            "Payment Received At": participant["paymentReceivedAt"],
            "Check-in Status": participant["checkInStatus"],
            "Checked In At": participant["checkedInAt"],
        }
        for participant in fetch_participants()
    ]
    return make_csv_response(rows, ["ID", "Full Name", "Age", "Gender", "Contact Number", "District", "Church", "Category", "Lecture", "Team", "Team Color", "Payment Status", "Payment Amount", "Payment Reference", "Payment Note", "Payment Received At", "Check-in Status", "Checked In At"])


def export_meal_stubs_csv():
    rows = [
        {
            "ID": f"MS-{meal_stub['id']:04d}",
            "Full Name": meal_stub["name"],
            "Age": meal_stub["age"],
            "Gender": meal_stub["gender"],
            "Contact Number": meal_stub["contactNumber"],
            "District": meal_stub["district"],
            "Church": meal_stub["church"],
            "Category": meal_stub["category"],
            "Meal Plan": ", ".join(meal_stub["mealPlan"]),
            "ID Jacket and Sling": "Yes" if meal_stub["idJacketSling"] else "No",
            "Claimed Meals": ", ".join(meal_stub["claimedMeals"]),
            "Date Registered": meal_stub["dateRegistered"],
        }
        for meal_stub in fetch_meal_stubs()
    ]
    return make_csv_response(rows, ["ID", "Full Name", "Age", "Gender", "Contact Number", "District", "Church", "Category", "Meal Plan", "ID Jacket and Sling", "Claimed Meals", "Date Registered"])


def fetch_payment_transactions():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT *
    FROM payment_transactions
    ORDER BY transaction_id DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "transactionId": row["transaction_id"],
            "id": row["id_number"],
            "name": row["participant_name"],
            "church": row["church_name"],
            "district": row["district"],
            "category": row["category"],
            "action": row["action"],
            "amount": float(row["amount"] or 0),
            "reference": row["reference"] or "",
            "note": row["note"] or "",
            "updatedBy": row["updated_by"] or "",
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def export_payment_transactions_csv():
    rows = [
        {
            "Transaction ID": transaction["transactionId"],
            "Participant ID": transaction["id"],
            "Full Name": transaction["name"],
            "Church": transaction["church"],
            "District": transaction["district"],
            "Category": transaction["category"],
            "Action": transaction["action"],
            "Amount": transaction["amount"],
            "Reference": transaction["reference"],
            "Note": transaction["note"],
            "Updated By": transaction["updatedBy"],
            "Created At": transaction["createdAt"],
        }
        for transaction in fetch_payment_transactions()
    ]
    return make_csv_response(rows, ["Transaction ID", "Participant ID", "Full Name", "Church", "District", "Category", "Action", "Amount", "Reference", "Note", "Updated By", "Created At"])


def get_backup_status():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_files = [
        os.path.join(BACKUP_DIR, filename)
        for filename in os.listdir(BACKUP_DIR)
        if filename.lower().endswith(".db")
    ]
    settings = load_shared_settings()
    base_status = {
        "autoBackupEnabled": is_auto_backup_enabled(settings),
        "autoBackupMinuteInterval": str(get_auto_backup_minutes(settings)),
        "backupCount": len(backup_files),
    }

    if not backup_files:
        return {**base_status, "latestBackup": "", "latestBackupAt": ""}

    latest_backup = max(backup_files, key=os.path.getmtime)
    return {
        **base_status,
        "latestBackup": os.path.basename(latest_backup),
        "latestBackupAt": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(latest_backup))),
    }


def fetch_print_history():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("""
    SELECT *
    FROM print_history
    ORDER BY history_id DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "historyId": row["history_id"],
            "id": row["id_number"],
            "name": row["participant_name"],
            "category": row["category"],
            "district": row["district"],
            "church": row["church_name"],
            "lecture": row["lecture"],
            "printMode": row["print_mode"],
            "batchSize": row["batch_size"],
            "status": row["status"] or "Done",
            "printedAt": row["printed_at"],
        }
        for row in rows
    ]


def get_network_access_info():
    ip_address = "127.0.0.1"

    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip_address = probe.getsockname()[0]
        probe.close()
    except OSError:
        try:
            ip_address = socket.gethostbyname(socket.gethostname())
        except OSError:
            ip_address = "127.0.0.1"

    return {
        "ipAddress": ip_address,
        "apiPort": PORT,
    }


def load_shared_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {}

    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            return json.load(settings_file)
    except (OSError, json.JSONDecodeError):
        return {}


def save_shared_settings(data):
    if not isinstance(data, dict):
        data = {}

    with open(SETTINGS_FILE, "w", encoding="utf-8") as settings_file:
        json.dump(data, settings_file, indent=2)

    return data


def create_print_history(data):
    participants = data.get("participants", [])
    print_mode = data.get("printMode", "Single")
    batch_size = len(participants)
    status = data.get("status", "Printing")

    if status not in PRINT_STATUSES:
        status = "Printing"

    conn = connect_db()
    cursor = conn.cursor()
    created_ids = []

    for participant in participants:
        cursor.execute("""
        INSERT INTO print_history (
            id_number, participant_name, category, district, church_name,
            lecture, print_mode, batch_size, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            participant.get("id", ""),
            participant.get("name", ""),
            participant.get("category", ""),
            participant.get("district", ""),
            participant.get("church", ""),
            participant.get("lecture", ""),
            print_mode,
            batch_size,
            status,
        ))
        created_ids.append(cursor.lastrowid)

    conn.commit()
    conn.close()
    return {"history": fetch_print_history(), "createdIds": created_ids}


def update_print_history_status(data):
    history_ids = data.get("historyIds", [])
    status = data.get("status", "Done")

    if status not in PRINT_STATUSES:
        status = "Done"

    conn = connect_db()
    cursor = conn.cursor()

    for history_id in history_ids:
        cursor.execute(
            "UPDATE print_history SET status = ? WHERE history_id = ?",
            (status, history_id),
        )

    conn.commit()
    conn.close()
    return fetch_print_history()


class ApiHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/participants":
            return self.send_json(fetch_participants())

        if parsed.path == "/api/health":
            return self.send_json({"status": "ok", "database": os.path.basename(get_active_db_path())})

        if parsed.path == "/api/network-access":
            return self.send_json(get_network_access_info())

        if parsed.path == "/api/settings":
            return self.send_json(load_shared_settings())

        if parsed.path == "/api/databases":
            return self.send_json(list_databases())

        if parsed.path == "/api/activity-log":
            return self.send_json(fetch_activity_log())

        if parsed.path == "/api/print-history":
            return self.send_json(fetch_print_history())

        if parsed.path == "/api/meal-stubs":
            return self.send_json(fetch_meal_stubs())

        if parsed.path == "/api/meal-claim-log":
            return self.send_json(fetch_meal_claim_log())

        if parsed.path == "/api/payment-transactions":
            return self.send_json(fetch_payment_transactions())

        if parsed.path == "/api/backup-status":
            return self.send_json(get_backup_status())

        if parsed.path == "/api/export/participants":
            return self.send_download(export_participants_csv(), "participants.csv", "text/csv; charset=utf-8")

        if parsed.path == "/api/export/meal-stubs":
            return self.send_download(export_meal_stubs_csv(), "meal-stub-records.csv", "text/csv; charset=utf-8")

        if parsed.path == "/api/export/payment-transactions":
            return self.send_download(export_payment_transactions_csv(), "payment-transactions.csv", "text/csv; charset=utf-8")

        if parsed.path.startswith("/photos/"):
            return self.send_file(unquote(parsed.path.lstrip("/")))

        self.send_error(404, "Not found")

    def do_POST(self):
        if self.path == "/api/print-history":
            history = create_print_history(self.read_json())
            self.send_json(history, status=201)
            return

        if self.path == "/api/meal-stubs":
            meal_stub = create_meal_stub(self.read_json())
            self.send_json(meal_stub, status=201)
            return

        if self.path == "/api/backup-database":
            self.send_json(backup_database(), status=201)
            return

        if self.path == "/api/restore-latest-backup":
            restored = restore_latest_backup()

            if restored is None:
                self.send_error(404, "No backup database found")
                return

            self.send_json(restored, status=201)
            return

        if self.path == "/api/databases":
            created = create_database()

            if created is None:
                self.send_error(400, "Database could not be created")
                return

            self.send_json(created, status=201)
            return

        if self.path == "/api/camera/ip-webcam/snapshot":
            try:
                snapshot = capture_ip_webcam_snapshot(self.read_json().get("url", ""))
            except Exception:
                snapshot = None

            if snapshot is None:
                self.send_error(400, "Could not capture a photo from the IP Webcam URL")
                return

            self.send_json(snapshot)
            return

        if self.path != "/api/participants":
            self.send_error(404, "Not found")
            return

        data = self.read_json()
        participant = create_participant(data)
        self.send_json(participant, status=201)

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/print-history/status":
            history = update_print_history_status(self.read_json())
            self.send_json(history)
            return

        if parsed.path == "/api/participants/check-in":
            self.send_json(update_bulk_participant_check_in(self.read_json()))
            return

        if parsed.path == "/api/participants/payment":
            self.send_json(update_bulk_participant_payment(self.read_json()))
            return

        if parsed.path == "/api/settings":
            self.send_json(save_shared_settings(self.read_json()))
            return

        if parsed.path == "/api/databases/activate":
            activated = activate_database(self.read_json().get("filename", ""))

            if activated is None:
                self.send_error(404, "Database file not found")
                return

            self.send_json(activated)
            return

        meal_stub_prefix = "/api/meal-stubs/"

        if parsed.path.startswith(meal_stub_prefix) and parsed.path.endswith("/claims"):
            meal_stub_id = unquote(parsed.path[len(meal_stub_prefix):-len("/claims")])
            meal_stub = update_meal_stub_claims(meal_stub_id, self.read_json())

            if meal_stub is None:
                self.send_error(404, "Meal stub record not found")
                return

            self.send_json(meal_stub)
            return

        if parsed.path.startswith(meal_stub_prefix):
            meal_stub_id = unquote(parsed.path[len(meal_stub_prefix):])
            meal_stub = update_meal_stub(meal_stub_id, self.read_json())

            if meal_stub is None:
                self.send_error(404, "Meal stub record not found")
                return

            self.send_json(meal_stub)
            return

        prefix = "/api/participants/"

        if not parsed.path.startswith(prefix):
            self.send_error(404, "Not found")
            return

        if parsed.path.endswith("/check-in"):
            id_number = unquote(parsed.path[len(prefix):-len("/check-in")])
            participant = update_participant_check_in(id_number, self.read_json().get("status", "Not Yet"))

            if participant is None:
                self.send_error(404, "Participant not found")
                return

            self.send_json(participant)
            return

        if parsed.path.endswith("/payment"):
            id_number = unquote(parsed.path[len(prefix):-len("/payment")])
            participant = update_participant_payment(id_number, self.read_json())

            if participant is None:
                self.send_error(404, "Participant not found")
                return

            self.send_json(participant)
            return

        id_number = unquote(parsed.path[len(prefix):])
        participant = update_participant(id_number, self.read_json())

        if participant is None:
            self.send_error(404, "Participant not found")
            return

        self.send_json(participant)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        meal_stub_prefix = "/api/meal-stubs/"

        if parsed.path.startswith(meal_stub_prefix):
            meal_stub_id = unquote(parsed.path[len(meal_stub_prefix):])
            conn = connect_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM meal_stub_registrations WHERE meal_stub_id = ?", (meal_stub_id,))
            deleted = cursor.rowcount
            conn.commit()
            conn.close()

            if not deleted:
                self.send_error(404, "Meal stub record not found")
                return

            log_activity("Meal Stub Deleted", "meal-stub", meal_stub_id, "")
            self.send_json({"deleted": True, "id": meal_stub_id})
            return

        prefix = "/api/participants/"

        if not parsed.path.startswith(prefix):
            self.send_error(404, "Not found")
            return

        id_number = unquote(parsed.path[len(prefix):])
        conn = connect_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM participants WHERE id_number = ?", (id_number,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        if not deleted:
            self.send_error(404, "Participant not found")
            return

        log_activity("Participant Deleted", "participant", id_number, "")
        self.send_json({"deleted": True, "id": id_number})

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "{}")

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_download(self, body, filename, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path):
        full_path = os.path.abspath(os.path.join(BASE_DIR, path))
        photo_dir = os.path.abspath(PHOTO_DIR)

        if not full_path.startswith(photo_dir + os.sep):
            self.send_error(403, "Forbidden")
            return

        if not os.path.exists(full_path):
            self.send_error(404, "File not found")
            return

        content_type = "image/png"
        if full_path.lower().endswith((".jpg", ".jpeg")):
            content_type = "image/jpeg"
        elif full_path.lower().endswith(".webp"):
            content_type = "image/webp"

        with open(full_path, "rb") as file:
            body = file.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ensure_database()
    start_auto_backup_worker()
    server = ThreadingHTTPServer((HOST, PORT), ApiHandler)
    print(f"API server running at http://{HOST}:{PORT}")
    server.serve_forever()
