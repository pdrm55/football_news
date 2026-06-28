import sqlite3
import logging
from contextlib import contextmanager

DB_FILE = "football_news.db"
logger = logging.getLogger("database")

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Database error: {e}")
        raise
    finally:
        conn.close()

def init_db():
    """Initializes tables and unique constraints in SQLite."""
    with get_db() as conn:
        cursor = conn.cursor()
        
        # Sources table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('rss', 'web_link', 'x_account')),
                value TEXT NOT NULL,
                team_tag TEXT NOT NULL CHECK(team_tag IN ('Arsenal', 'Liverpool', 'Inter')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(type, value, team_tag)
            )
        """)
        
        # Filters table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS filters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # News articles table (with 'skipped' constraint)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NULL,
                unique_identifier TEXT UNIQUE NOT NULL,
                original_title TEXT,
                original_content TEXT,
                media_url TEXT NULL,
                ai_summary TEXT NULL,
                team_tag TEXT NULL,
                status TEXT CHECK(status IN ('pending', 'processed', 'sent', 'skipped')) DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE SET NULL
            )
        """)
        
        # Self-migration: Add team_tag column if it doesn't exist yet
        cursor.execute("PRAGMA table_info(news_articles)")
        columns = [row[1] for row in cursor.fetchall()]
        if 'team_tag' not in columns:
            cursor.execute("ALTER TABLE news_articles ADD COLUMN team_tag TEXT NULL")
            
        # Self-migration: Check constraint update to support 'skipped'
        try:
            cursor.execute("SAVEPOINT migration_test")
            try:
                cursor.execute(
                    "INSERT INTO news_articles (unique_identifier, status) VALUES ('migration_test_chk', 'skipped')"
                )
                cursor.execute("ROLLBACK TO migration_test")
            except sqlite3.IntegrityError:
                logger.info("Migrating news_articles table to support 'skipped' status...")
                cursor.execute("ROLLBACK TO migration_test")
                
                # 1. Rename old table
                cursor.execute("ALTER TABLE news_articles RENAME TO old_news_articles")
                
                # 2. Create the new table with updated CHECK constraint
                cursor.execute("""
                    CREATE TABLE news_articles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_id INTEGER NULL,
                        unique_identifier TEXT UNIQUE NOT NULL,
                        original_title TEXT,
                        original_content TEXT,
                        media_url TEXT NULL,
                        ai_summary TEXT NULL,
                        team_tag TEXT NULL,
                        status TEXT CHECK(status IN ('pending', 'processed', 'sent', 'skipped')) DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE SET NULL
                    )
                """)
                
                # 3. Copy data
                cursor.execute("PRAGMA table_info(old_news_articles)")
                old_cols = [row[1] for row in cursor.fetchall()]
                common_cols = [c for c in old_cols if c in [
                    'id', 'source_id', 'unique_identifier', 'original_title', 
                    'original_content', 'media_url', 'ai_summary', 'team_tag', 
                    'status', 'created_at'
                ]]
                col_list_str = ", ".join(common_cols)
                cursor.execute(f"""
                    INSERT INTO news_articles ({col_list_str})
                    SELECT {col_list_str} FROM old_news_articles
                """)
                
                # 4. Drop old table
                cursor.execute("DROP TABLE old_news_articles")
                logger.info("Database migration to 'skipped' status completed successfully.")
            finally:
                cursor.execute("RELEASE migration_test")
        except Exception as migration_err:
            logger.error(f"Migration error for 'skipped' status: {migration_err}")
            
    logger.info("Database initialized successfully.")

# CRUD for Sources
def add_source(source_type: str, value: str, team_tag: str) -> bool:
    """Adds a new source. Returns True if successful, False if already exists."""
    try:
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO sources (type, value, team_tag) VALUES (?, ?, ?)",
                (source_type, value.strip(), team_tag)
            )
            return True
    except sqlite3.IntegrityError:
        logger.warning(f"Source already exists: {source_type} - {value} ({team_tag})")
        return False

def remove_source(source_id: int) -> bool:
    """Removes a source by id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return cursor.rowcount > 0

def get_sources():
    """Returns a list of all sources as dicts."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sources ORDER BY team_tag, type")
        return [dict(row) for row in cursor.fetchall()]

# CRUD for Filters
def add_filter(keyword: str) -> bool:
    """Adds a filter keyword (stored lowercase). Returns True if added, False if duplicate."""
    try:
        with get_db() as conn:
            conn.cursor().execute(
                "INSERT INTO filters (keyword) VALUES (?)",
                (keyword.strip().lower(),)
            )
            return True
    except sqlite3.IntegrityError:
        logger.warning(f"Filter already exists: {keyword}")
        return False

def remove_filter(filter_id: int) -> bool:
    """Removes a filter by id."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM filters WHERE id = ?", (filter_id,))
        return cursor.rowcount > 0

def get_filters():
    """Returns all active filter keywords."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM filters ORDER BY keyword")
        return [dict(row) for row in cursor.fetchall()]

# CRUD for News Articles
def save_article(source_id: int | None, unique_identifier: str, original_title: str, original_content: str, media_url: str | None, team_tag: str | None = None) -> int | None:
    """Saves a new article with status 'pending'. Returns the auto-increment ID if saved, None if already exists."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO news_articles 
                   (source_id, unique_identifier, original_title, original_content, media_url, team_tag, status) 
                   VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (source_id, unique_identifier, original_title, original_content, media_url, team_tag)
            )
            return cursor.lastrowid
    except sqlite3.IntegrityError:
        return None

def article_exists(unique_identifier: str) -> bool:
    """Checks if an article with unique_identifier exists."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM news_articles WHERE unique_identifier = ?", (unique_identifier,))
        return cursor.fetchone() is not None

def get_pending_articles():
    """Returns all articles in 'pending' status."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT a.*, COALESCE(a.team_tag, s.team_tag) AS team_tag, s.type AS source_type 
               FROM news_articles a 
               LEFT JOIN sources s ON a.source_id = s.id 
               WHERE a.status = 'pending'"""
        )
        return [dict(row) for row in cursor.fetchall()]

def get_processed_articles():
    """Returns all articles in 'processed' status (summarized but not yet sent)."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """SELECT a.*, COALESCE(a.team_tag, s.team_tag) AS team_tag, s.type AS source_type 
               FROM news_articles a 
               LEFT JOIN sources s ON a.source_id = s.id 
               WHERE a.status = 'processed'"""
        )
        return [dict(row) for row in cursor.fetchall()]

def update_article_summary_status(article_id: int, ai_summary: str, status: str):
    """Updates the summary and status of an article."""
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE news_articles SET ai_summary = ?, status = ? WHERE id = ?",
            (ai_summary, status, article_id)
        )

def update_article_status(article_id: int, status: str):
    """Updates the status of an article."""
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE news_articles SET status = ? WHERE id = ?",
            (status, article_id)
        )
