"""
Public routes cho electron-updater (generic): latest.yml + file .exe theo từng app.

- Mới: https://<host>/api/desktop-updates/<app_slug>/latest.yml
- Tương thích: https://<host>/api/app-desktop/latest.yml → bản tiengcuoi-dong
"""
from urllib.parse import unquote

from flask import Blueprint, Response, abort, stream_with_context

from db import get_db
from upload import _validate_app_slug, stream_desktop_installer

desktop_public_bp = Blueprint('desktop_public', __name__)
desktop_legacy_bp = Blueprint('desktop_legacy', __name__)


def _get_latest_release_row(app_slug: str):
    slug = _validate_app_slug(app_slug)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT installer_filename, installer_object_key, yml_content, installer_size
        FROM desktop_app_releases
        WHERE app_slug = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (slug,),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None


def _serve_latest_yml(app_slug: str):
    try:
        row = _get_latest_release_row(app_slug)
    except ValueError:
        abort(404)
    if not row:
        abort(404)
    yml = row['yml_content']
    resp = Response(yml, mimetype='text/yaml; charset=utf-8')
    resp.headers['Cache-Control'] = 'public, max-age=120'
    return resp


def _serve_installer(app_slug: str, filename: str):
    try:
        row = _get_latest_release_row(app_slug)
    except ValueError:
        abort(404)
    if not row:
        abort(404)
    filename = unquote(filename).strip()
    if filename != row['installer_filename']:
        abort(404)

    try:
        obj = stream_desktop_installer(row['installer_object_key'])
    except Exception:
        abort(404)

    def generate():
        try:
            while True:
                chunk = obj.read(32 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                obj.close()
                obj.release_conn()
            except Exception:
                pass

    size = int(row['installer_size'] or 0)
    headers = {
        'Content-Type': 'application/vnd.microsoft.portable-executable',
        'Content-Disposition': f'attachment; filename="{filename}"',
    }
    if size > 0:
        headers['Content-Length'] = str(size)
    resp = Response(stream_with_context(generate()), headers=headers)
    resp.headers['Cache-Control'] = 'public, max-age=3600'
    return resp


# ─── /api/desktop-updates/<slug>/... ───────────────────────────────────────

@desktop_public_bp.route('/<app_slug>/latest.yml', methods=['GET'])
def serve_latest_yml_multi(app_slug):
    return _serve_latest_yml(app_slug)


@desktop_public_bp.route('/<app_slug>/<path:filename>', methods=['GET'])
def serve_installer_multi(app_slug, filename):
    if filename == 'latest.yml':
        abort(404)
    return _serve_installer(app_slug, filename)


# ─── Legacy: /api/app-desktop/... → tiengcuoi-dong ─────────────────────────

@desktop_legacy_bp.route('/latest.yml', methods=['GET'])
def serve_latest_yml_legacy():
    return _serve_latest_yml('tiengcuoi-dong')


@desktop_legacy_bp.route('/<path:filename>', methods=['GET'])
def serve_installer_legacy(filename):
    if filename == 'latest.yml':
        abort(404)
    return _serve_installer('tiengcuoi-dong', filename)
