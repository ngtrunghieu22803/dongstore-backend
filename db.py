"""
Database: PostgreSQL (chạy trên cùng máy với backend - localhost).
Cấu hình qua biến môi trường hoặc DATABASE_URL trong .env.
"""
import json
import os
import uuid
import bcrypt


def get_admin_seed_password() -> str:
    """Mật khẩu seed cho adminDongstore; ghi đè bằng biến môi trường ADMIN_SEED_PASSWORD."""
    return os.environ.get("ADMIN_SEED_PASSWORD", "zxjkfhcjkshdioye897(*^&(*&*()@YHoghIH")


def _pg_dsn() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if url:
        return url
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    db = os.environ.get("PG_DATABASE", "dong_store")
    user = os.environ.get("PG_USER", "Dong_store")
    password = os.environ.get("PG_PASSWORD", "fMBsnrH8jLjpFnXs")
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def get_db():
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(_pg_dsn(), cursor_factory=RealDictCursor)
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT DEFAULT '',
            role TEXT DEFAULT 'user',
            google_id TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price INTEGER NOT NULL,
            original_price INTEGER,
            badge TEXT,
            emoji TEXT,
            description TEXT,
            features TEXT,
            image TEXT,
            video_url TEXT,
            stock INTEGER DEFAULT -1,
            is_active INTEGER DEFAULT 1,
            content TEXT,
            images TEXT,
            install_guide TEXT,
            screenshots TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    # Thêm cột image / video_url nếu chưa có (migration cho DB cũ)
    for col_sql in [
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS image TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS video_url TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS content TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS images TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS install_guide TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS screenshots TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS preview_audio TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS duration_options TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS duration_prices TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS require_duration INTEGER DEFAULT 0",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS app_download_url TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS app_installer_path TEXT",
    ]:
        cur.execute(col_sql)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sounds (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT DEFAULT 'Sounds',
            description TEXT,
            object_name TEXT NOT NULL,
            original_filename TEXT,
            file_size INTEGER,
            duration_seconds INTEGER,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute("ALTER TABLE sounds ADD COLUMN IF NOT EXISTS storage TEXT DEFAULT 'minio'")
    cur.execute("ALTER TABLE sounds ADD COLUMN IF NOT EXISTS price INTEGER DEFAULT 0")

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            product_id TEXT NOT NULL REFERENCES products(id),
            product_snapshot TEXT,
            total_price INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            payment_method TEXT,
            payment_id TEXT,
            license_key TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            token_hash TEXT NOT NULL,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # Wallets - mỗi user có 1 ví
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL UNIQUE REFERENCES users(id),
            balance INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    # Deposit requests - lịch sử nạp tiền
    cur.execute("""
        CREATE TABLE IF NOT EXISTS deposits (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id),
            amount INTEGER NOT NULL,
            transfer_content TEXT NOT NULL,
            bank_name TEXT,
            account_number TEXT,
            account_name TEXT,
            qr_url TEXT,
            status TEXT DEFAULT 'pending',
            vietqr_id TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            confirmed_at TIMESTAMPTZ,
            cancelled_at TIMESTAMPTZ
        )
    """)

    # Licenses table - lưu trữ license key, secret, và binding info
    cur.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id TEXT PRIMARY KEY,
            license_key TEXT UNIQUE NOT NULL,
            license_secret_hash TEXT NOT NULL,
            product_id TEXT NOT NULL REFERENCES products(id),
            user_id TEXT REFERENCES users(id),
            machine_id TEXT,
            machine_fingerprint TEXT,
            status TEXT DEFAULT 'active',
            activated_at TIMESTAMPTZ DEFAULT NOW(),
            expires_at TIMESTAMPTZ,
            last_check_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)

    cur.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS display_title TEXT")
    cur.execute("ALTER TABLE licenses ADD COLUMN IF NOT EXISTS free_hwid_reset_after TIMESTAMPTZ")

    # Index cho licenses
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_licenses_license_key ON licenses(license_key)
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_licenses_user_id ON licenses(user_id)
    """)

    # Âm thanh đã thêm vào app (xác nhận từ Electron sau khi tải OK — đồng bộ với web)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sound_imports (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            product_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (user_id, product_id)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_user_sound_imports_user
        ON user_sound_imports(user_id)
        """
    )

    # Đăng ký ứng dụng desktop (Electron) — mỗi app một slug, feed riêng
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS desktop_apps (
            id TEXT PRIMARY KEY,
            slug TEXT UNIQUE NOT NULL,
            display_name TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )

    # Bản cài (electron-updater: latest.yml + NSIS exe) — theo từng app_slug
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS desktop_app_releases (
            id TEXT PRIMARY KEY,
            app_slug TEXT NOT NULL,
            version TEXT NOT NULL,
            notes TEXT,
            installer_filename TEXT NOT NULL,
            installer_object_key TEXT NOT NULL,
            installer_size BIGINT NOT NULL,
            installer_sha512 TEXT NOT NULL,
            yml_content TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        )
        """
    )
    cur.execute(
        "ALTER TABLE desktop_app_releases ADD COLUMN IF NOT EXISTS app_slug TEXT"
    )
    cur.execute(
        """
        UPDATE desktop_app_releases
        SET app_slug = 'tiengcuoi-dong'
        WHERE app_slug IS NULL OR TRIM(app_slug) = ''
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_desktop_releases_app_created
        ON desktop_app_releases(app_slug, created_at DESC)
        """
    )

    # Seed app mặc định (có thể thêm app khác trong admin)
    cur.execute("SELECT id FROM desktop_apps WHERE slug = %s", ("tiengcuoi-dong",))
    if not cur.fetchone():
        cur.execute(
            """
            INSERT INTO desktop_apps (id, slug, display_name)
            VALUES (%s, %s, %s)
            """,
            (str(uuid.uuid4()), "tiengcuoi-dong", "Tiếng Cười Động"),
        )

    conn.commit()

    cur.execute("SELECT COUNT(*) AS c FROM products")
    row = cur.fetchone()
    cnt = row["c"] if isinstance(row, dict) else row[0]
    if cnt == 0:
        seed_products(cur)

    # Luôn tạo admin mặc định nếu CHƯA có user nào trùng email (kể cả khi đã có admin khác).
    # Trước đây chỉ insert khi admin_count==0 → nếu DB đã có admin khác thì adminDongstore không bao giờ được tạo.
    cur.execute("SELECT id FROM users WHERE LOWER(email) = %s", ("admindongstore",))
    if not cur.fetchone():
        seed = get_admin_seed_password()
        password_hash = bcrypt.hashpw(seed.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        cur.execute(
            "INSERT INTO users (id, email, password, name, role) VALUES (%s, %s, %s, %s, %s)",
            (str(uuid.uuid4()), "adminDongstore", password_hash, "Quản trị viên", "admin"),
        )
        print("✅ Default admin account: adminDongstore / (ADMIN_SEED_PASSWORD hoặc mặc định trong get_admin_seed_password)")

    # Đồng bộ âm thanh có giá → products (checkout dùng cùng id)
    try:
        sound_cat = "Âm thanh"
        cur.execute(
            """
            SELECT id, name, description, price, is_active
            FROM sounds
            WHERE COALESCE(price, 0) > 0 AND COALESCE(is_active, 1) = 1
            """
        )
        for srow in cur.fetchall():
            sd = dict(srow)
            cur.execute(
                """
                INSERT INTO products (id, name, category, price, description, features, stock, is_active, require_duration, emoji)
                VALUES (%s, %s, %s, %s, %s, '[]', -1, 1, 0, '🎵')
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    category = EXCLUDED.category,
                    price = EXCLUDED.price,
                    description = COALESCE(EXCLUDED.description, products.description),
                    is_active = 1,
                    require_duration = 0
                """,
                (
                    sd["id"],
                    sd["name"],
                    sound_cat,
                    int(sd["price"]),
                    sd.get("description") or "",
                ),
            )
    except Exception as ex:
        print("⚠️  Backfill sound → products:", ex)

    conn.commit()
    cur.close()
    conn.close()
    print("✅ Database initialized (PostgreSQL)")


def seed_products(cur):
    products = [
        (
            "tc001",
            "Tiếng Cười Đông",
            "Phần mềm",
            99000,
            149000,
            "Bán chạy",
            "🎵",
            "Phần mềm phát âm thanh cưới hỏi tự động khi có quà tặng TikTok Live",
            json.dumps(["50+ âm thanh có sẵn", "Kết nối TikTok Live", "Hỗ trợ 24/7"]),
        ),
        (
            "vps01",
            "VPS Cấu Hình Cao",
            "Server",
            99000,
            None,
            "Mới",
            "🖥️",
            "Máy chủ ảo hiệu năng cao, phù hợp cho dự án nhỏ & vừa",
            json.dumps(["CPU 2 vCore", "RAM 4GB", "SSD 50GB NVMe"]),
        ),
        (
            "host01",
            "Hosting Premium",
            "Web Hosting",
            49000,
            89000,
            "Giảm 45%",
            "🌐",
            "Hosting tốc độ cao, hỗ trợ PHP, Node.js, Python, databases",
            json.dumps(["SSD 20GB", "Domain miễn phí", "SSL miễn phí"]),
        ),
        (
            "key001",
            "Key Bản Quyền Windows",
            "License",
            199000,
            499000,
            "Rẻ nhất",
            "🔑",
            "Key bản quyền Windows 10/11 Pro, kích hoạt vĩnh viễn",
            json.dumps(["Bản quyền vĩnh viễn", "Kích hoạt ngay", "Hỗ trợ Microsoft"]),
        ),
        (
            "mc001",
            "Minecraft Server",
            "Game Server",
            79000,
            None,
            None,
            "🎮",
            "Server Minecraft riêng, cấu hình linh hoạt, uptime 99.9%",
            json.dumps(["RAM 2-8GB tùy chọn", "Panel chủ động", "Plugin count"]),
        ),
        (
            "acc001",
            "Tài Khoản Cloud Storage",
            "Cloud",
            39000,
            59000,
            None,
            "☁️",
            "Tài khoản Google Drive, OneDrive, Dropbox dung lượng lớn giá rẻ",
            json.dumps(["5TB - 10TB storage", "Shareable", "Bảo hành 30 ngày"]),
        ),
        (
            "vpn001",
            "VPN Premium 1 Năm",
            "VPN",
            149000,
            299000,
            "Khuyến mãi",
            "🔒",
            "Truy cập internet an toàn, bảo mật, không giới hạn băng thông",
            json.dumps(["Không giới hạn", "50+ quốc gia", "Bảo hành 365 ngày"]),
        ),
        (
            "spot001",
            "Tài Khoản Spotify Premium",
            "Nhạc",
            29000,
            59000,
            None,
            "🎧",
            "Tài khoản Spotify Premium, nghe nhạc không quảng cáo, tải offline",
            json.dumps(["1 năm bảo hành", "Không quảng cáo", "Download offline"]),
        ),
    ]

    cur.executemany(
        """
        INSERT INTO products (id, name, category, price, original_price, badge, emoji, description, features, image, video_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """,
        [(p + (None, None)) for p in products],
    )
    print("✅ Products seeded")
