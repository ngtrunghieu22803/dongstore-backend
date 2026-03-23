# DONG STORE - Backend API (Python Flask)

Backend Flask cho cửa hàng sản phẩm số DONG STORE.

## Công nghệ

- **Runtime:** Python 3.10+
- **Framework:** Flask
- **Database:** SQLite
- **Auth:** JWT (Access Token + Refresh Token)
- **Password:** bcrypt

## Cài đặt

```bash
cd server-ban-key
pip install -r requirements.txt
```

## Chạy server

```bash
# Development (auto-reload)
python app.py

# Hoặc dùng flask CLI
flask --app app.py run --port 3001 --debug
```

Server sẽ chạy tại: `http://localhost:3001`

## Cấu trúc thư mục

```
server-ban-key/
├── app.py              # Entry point, Flask app
├── db.py               # SQLite setup + seed 8 sản phẩm
├── auth.py             # JWT helpers, require_auth decorator
├── routes/
│   ├── auth.py         # /api/auth/*
│   ├── products.py     # /api/products/*
│   ├── orders.py       # /api/orders/*
│   └── payments.py      # /api/payments/*
├── data/               # SQLite database (auto-created)
├── .env                # Environment variables
├── .env.example
└── requirements.txt
```

## API Endpoints

### Health Check
- `GET /api/health` - Kiểm tra server online

### Auth (`/api/auth`)
- `POST /register` - Đăng ký tài khoản mới
- `POST /login` - Đăng nhập
- `POST /refresh` - Làm mới token
- `POST /logout` - Đăng xuất (cần token)
- `GET /me` - Thông tin user hiện tại (cần token)
- `GET /google` - OAuth Google (placeholder)

### Products (`/api/products`)
- `GET /` - Danh sách sản phẩm (`?category=`)
- `GET /categories` - Danh mục sản phẩm
- `GET /:id` - Chi tiết sản phẩm

### Orders (`/api/orders`) — Cần token
- `GET /` - Danh sách đơn hàng của user
- `GET /:id` - Chi tiết đơn hàng
- `POST /` - Tạo đơn hàng

### Payments (`/api/payments`) — Cần token
- `POST /create-order` - Tạo đơn hàng thanh toán
- `POST /callback` - Webhook từ cổng thanh toán
- `POST /confirm` - Xác nhận chuyển khoản

## Database

- PostgreSQL (cấu hình qua `DATABASE_URL` hoặc `PG_*` trong `.env`)
- 8 sản phẩm seed sẵn
- Tables: `users`, `products`, `orders`, `sessions`

### Tạo lại admin `adminDongstore`

Nếu đã xóa user admin trong DB: **khởi động lại API** (`python app.py` hoặc process production). Mỗi lần chạy, `init_db()` sẽ **tự INSERT** user `adminDongstore` khi chưa có dòng `LOWER(email) = 'admindongstore'`.

Mật khẩu: mặc định trong `db.get_admin_seed_password()` hoặc ghi đè bằng biến môi trường **`ADMIN_SEED_PASSWORD`** trong `.env`.

**Lưu ý:** Nếu bảng `orders` còn dòng tham chiếu `user_id` cũ trong khi `users` đã trống, PostgreSQL có thể báo lỗi FK — cần xử lý đơn hàng cũ hoặc để DB sạch phù hợp.

## Biến môi trường

Copy `.env.example` thành `.env`:

```
FLASK_ENV=development
SECRET_KEY=your_secret_key
JWT_SECRET=your_jwt_secret
FRONTEND_URL=http://localhost:5173
```

## Frontend

Frontend chạy ở `http://localhost:5173`, gọi API tại `http://localhost:3001`.
