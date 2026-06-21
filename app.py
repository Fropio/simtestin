import sqlite3
import json
import requests
from flask import Flask, render_template, request, jsonify, send_file, session
from datetime import datetime
import io
import wikipedia
import os
import re
import time
import random
import secrets
from dotenv import load_dotenv
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

# Проверка наличия psycopg2
try:
    import psycopg2
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False
# ===== ПОДДЕРЖКА POSTGRESQL =====
try:
    import psycopg2
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', secrets.token_hex(32))

# ===== НАСТРОЙКИ БЕЗОПАСНОСТИ =====
app.config['SESSION_COOKIE_SECURE'] = os.getenv('HTTPS_ENABLED', 'false').lower() == 'true'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 3600

API_KEY = os.getenv('YANDEX_API_KEY')
FOLDER_ID = os.getenv('YANDEX_FOLDER_ID')
IAM_TOKEN = os.getenv('YANDEX_IAM_TOKEN')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')

if not FOLDER_ID:
    raise ValueError("❌ Не задан YANDEX_FOLDER_ID")
if not API_KEY and not IAM_TOKEN:
    raise ValueError("❌ Не задан ни YANDEX_API_KEY, ни YANDEX_IAM_TOKEN")


def get_db_connection():
    db_url = os.getenv('DATABASE_URL')

    # Если есть DATABASE_URL и psycopg2 установлен - используем PostgreSQL
    if db_url and HAS_POSTGRES and (db_url.startswith('postgres://') or db_url.startswith('postgresql://')):
        conn = psycopg2.connect(db_url)
        return conn
    else:
        # Иначе SQLite (для локальной разработки)
        conn = sqlite3.connect('database.db', timeout=30.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT,
            password_hash TEXT NOT NULL,
            email TEXT,
            role TEXT DEFAULT 'student',
            group_id INTEGER,
            subject TEXT,
            created_at TEXT,
            last_login TEXT,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (group_id) REFERENCES groups (id)
        )''')
        print("✅ users OK")

        cursor.execute('''CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            description TEXT,
            teacher_id INTEGER,
            created_at TEXT,
            FOREIGN KEY (teacher_id) REFERENCES users (id)
        )''')
        print("✅ groups OK")

        cursor.execute('''CREATE TABLE IF NOT EXISTS tests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            topic TEXT NOT NULL,
            difficulty TEXT DEFAULT 'средняя',
            teacher_id INTEGER NOT NULL,
            question_count INTEGER DEFAULT 5,
            question_types TEXT,
            status TEXT DEFAULT 'draft',
            created_at TEXT,
            published_at TEXT,
            FOREIGN KEY (teacher_id) REFERENCES users (id)
        )''')
        print("✅ tests OK")

        cursor.execute('''CREATE TABLE IF NOT EXISTS test_questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            question_text TEXT NOT NULL,
            question_type TEXT NOT NULL,
            options TEXT,
            correct_answer TEXT,
            correct_answers TEXT,
            expected_answer TEXT,
            points INTEGER DEFAULT 1,
            question_order INTEGER,
            FOREIGN KEY (test_id) REFERENCES tests (id)
        )''')
        print("✅ test_questions OK")

        cursor.execute('''CREATE TABLE IF NOT EXISTS test_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            assigned_at TEXT,
            due_date TEXT,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (test_id) REFERENCES tests (id),
            FOREIGN KEY (group_id) REFERENCES groups (id)
        )''')
        print("✅ test_assignments OK")

        cursor.execute('''CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            type TEXT,
            related_id INTEGER,
            is_read INTEGER DEFAULT 0,
            created_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )''')
        print("✅ notifications OK")

        cursor.execute('''CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            test_id INTEGER,
            student_name TEXT,
            score INTEGER,
            total_points INTEGER,
            date TEXT,
            answers_data TEXT,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (test_id) REFERENCES tests (id)
        )''')
        print("✅ results OK")

        cursor.execute("PRAGMA table_info(users)")
        users_cols = [col[1] for col in cursor.fetchall()]
        for col in ['full_name', 'subject', 'group_id', 'is_active']:
            if col not in users_cols:
                if col == 'is_active':
                    cursor.execute("ALTER TABLE users ADD COLUMN is_active INTEGER DEFAULT 1")
                else:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
                print(f"✅ Добавлена колонка users.{col}")

        cursor.execute("PRAGMA table_info(tests)")
        tests_cols = [col[1] for col in cursor.fetchall()]
        if 'status' not in tests_cols:
            cursor.execute("ALTER TABLE tests ADD COLUMN status TEXT DEFAULT 'draft'")
            print("✅ Добавлена колонка tests.status")
        if 'published_at' not in tests_cols:
            cursor.execute("ALTER TABLE tests ADD COLUMN published_at TEXT")
            print("✅ Добавлена колонка tests.published_at")

        cursor.execute("PRAGMA table_info(results)")
        results_cols = [col[1] for col in cursor.fetchall()]
        if 'test_id' not in results_cols:
            cursor.execute("ALTER TABLE results ADD COLUMN test_id INTEGER")
            print("✅ Добавлена колонка results.test_id")

        for sql in [
            'CREATE INDEX IF NOT EXISTS idx_username ON users(username)',
            'CREATE INDEX IF NOT EXISTS idx_users_role ON users(role)',
            'CREATE INDEX IF NOT EXISTS idx_users_group ON users(group_id)',
            'CREATE INDEX IF NOT EXISTS idx_tests_teacher ON tests(teacher_id)',
            'CREATE INDEX IF NOT EXISTS idx_tests_status ON tests(status)',
            'CREATE INDEX IF NOT EXISTS idx_assignments_group ON test_assignments(group_id)',
            'CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, is_read)',
            'CREATE INDEX IF NOT EXISTS idx_results_test ON results(test_id)',
        ]:
            try:
                cursor.execute(sql)
            except:
                pass

        cursor.execute("SELECT COUNT(*) FROM users WHERE username='admin'")
        if cursor.fetchone()[0] == 0 and ADMIN_PASSWORD:
            cursor.execute('''INSERT INTO users (username, full_name, password_hash, email, role, created_at) 
                VALUES (?, ?, ?, ?, ?, ?)''',
                           ('admin', 'Администратор', generate_password_hash(ADMIN_PASSWORD),
                            'admin@simtestin.local', 'admin', datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            print("✅ Админ создан: username='admin', password=ADMIN_PASSWORD")

        conn.commit()
        print("✅ БД инициализирована")
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Требуется авторизация"}), 401
        return f(*args, **kwargs)

    return decorated


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if session.get('role') not in roles:
                return jsonify({"error": f"Доступ только для: {', '.join(roles)}"}), 403
            return f(*args, **kwargs)

        return decorated

    return decorator


def _check_admin():
    if session.get('role') == 'admin':
        return True
    pwd = request.headers.get('X-Admin-Password') or request.args.get('admin_password')
    return bool(ADMIN_PASSWORD and pwd == ADMIN_PASSWORD)


def require_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _check_admin():
            return jsonify({"error": "Доступ запрещён. Требуется роль администратора."}), 403
        return f(*args, **kwargs)

    return decorated


def generate_test_via_ai(topic_content, difficulty, count, question_types=None):
    """Генерация теста через YandexGPT с полной диагностикой"""
    url = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"

    # === ДИАГНОСТИКА ===
    print(f"\n{'=' * 60}")
    print(f"🤖 Запрос к YandexGPT")
    print(f"📁 FOLDER_ID: {FOLDER_ID[:10]}...{FOLDER_ID[-5:]}" if FOLDER_ID else "❌ FOLDER_ID пустой!")
    print(f"🔑 API_KEY: {'✅ ' + API_KEY[:8] + '...' if API_KEY else '❌ Не задан'}")
    print(f"🔑 IAM_TOKEN: {'✅ задан' if IAM_TOKEN else '❌ Не задан'}")
    print(f"📝 Тема: {topic_content[:100]}...")
    print(f"{'=' * 60}\n")

    if not FOLDER_ID:
        return {"error": "Не задан FOLDER_ID"}
    if not API_KEY and not IAM_TOKEN:
        return {"error": "Не задан API_KEY или IAM_TOKEN"}

    if not question_types:
        question_types = {'single': 1}

    type_names = {
        'single': 'один правильный ответ (4 варианта, correct_index: 0-3)',
        'multiple': 'несколько правильных (5 вариантов, correct_answers: [0,2])',
        'text': 'краткий ответ (expected_answer: "слово")'
    }

    allowed_types = [t for t in question_types.keys() if t in type_names]
    if not allowed_types:
        allowed_types = ['single']

    types_instruction = "Типы вопросов: " + ", ".join([type_names[t] for t in allowed_types]) + "."

    total_weight = sum(question_types.get(t, 0) for t in allowed_types)
    questions_by_type = {}
    remaining = count
    for qtype in allowed_types:
        weight = question_types.get(qtype, 1)
        qty = max(1, int(count * weight / total_weight)) if total_weight > 0 else 1
        questions_by_type[qtype] = min(qty, remaining)
        remaining -= qty
    if remaining > 0 and questions_by_type:
        first_type = next(iter(questions_by_type))
        questions_by_type[first_type] += remaining

    prompt = f"""Ты эксперт по созданию тестов. На основе: "{topic_content}"
Создай тест из {count} вопросов сложности "{difficulty}".
{types_instruction}

🔒 СТРОГИЕ ПРАВИЛА:
1. Генерируй ТОЛЬКО типы: {', '.join(allowed_types)}
2. Для 'single': 4 варианта, correct_index = ИНДЕКС правильного (0-3)
3. Для 'multiple': 5 вариантов, correct_answers = [индексы правильных, минимум 2]
4. Для 'text': expected_answer = правильный ответ (1-5 слов)
5. ВСЕГДА заполняй поля правильных ответов!

Верни ТОЛЬКО JSON:
{{"questions": [{{"question": "...", "type": "single|multiple|text", "options": [], "correct_index": 0, "correct_answers": [], "expected_answer": "...", "points": 1}}]}}"""

    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = f"Api-Key {API_KEY}"
    else:
        headers["Authorization"] = f"Bearer {IAM_TOKEN}"

    data = {
        "modelUri": f"gpt://{FOLDER_ID}/yandexgpt-lite/latest",
        "completionOptions": {"stream": False, "temperature": 0.6, "maxTokens": 4000},
        "messages": [{"role": "system", "text": "Ты — эксперт-методист, создающий тесты."},
                     {"role": "user", "text": prompt}]
    }

    try:
        print(f"📤 Отправляю запрос в YandexGPT...")
        resp = requests.post(url, headers=headers, json=data, timeout=90)

        # === ПОЛНАЯ ДИАГНОСТИКА ОТВЕТА ===
        print(f"📥 Статус ответа: {resp.status_code}")
        print(f"📥 Заголовки: {dict(resp.headers)}")

        if resp.status_code != 200:
            print(f"❌ ОШИБКА API!")
            print(f"❌ Текст ошибки: {resp.text}")
            try:
                error_json = resp.json()
                print(f"❌ JSON ошибки: {json.dumps(error_json, ensure_ascii=False, indent=2)}")
                error_msg = error_json.get('error', {}).get('message', resp.text)
            except:
                error_msg = resp.text
            return {"error": f"API {resp.status_code}: {error_msg}"}

        result = resp.json()
        print(f"✅ Успешный ответ от YandexGPT")

        text = result['result']['alternatives'][0]['message']['text']
        match = re.search(r'\{.*\}', text, re.DOTALL)
        parsed = json.loads(match.group(0) if match else text)

        if "questions" not in parsed:
            return {"error": "Нет 'questions' в ответе"}

        filtered = [q for q in parsed["questions"] if q.get('type') in allowed_types]

        for q in filtered:
            q.setdefault('type', 'single')
            q.setdefault('points', 1)

            if q['type'] == 'single':
                if not q.get('options') or len(q['options']) != 4:
                    q['options'] = ['Вариант А', 'Вариант Б', 'Вариант В', 'Вариант Г']
                correct_idx = q.get('correct_index')
                if correct_idx is None or not isinstance(correct_idx,
                                                         (int, float)) or correct_idx < 0 or correct_idx > 3:
                    q['correct_index'] = 0
                q['correct_answer'] = int(q['correct_index'])

            elif q['type'] == 'multiple':
                if not q.get('options') or len(q['options']) != 5:
                    q['options'] = ['Вариант 1', 'Вариант 2', 'Вариант 3', 'Вариант 4', 'Вариант 5']
                correct_ans = q.get('correct_answers')
                if not correct_ans or not isinstance(correct_ans, list) or len(correct_ans) < 2:
                    q['correct_answers'] = [0, 1]
                q['correct_answers'] = [int(i) for i in q['correct_answers'] if 0 <= i < 5]
                if len(q['correct_answers']) < 2:
                    q['correct_answers'] = [0, 1]

            elif q['type'] == 'text':
                if not q.get('expected_answer') or not isinstance(q['expected_answer'], str) or len(
                        q['expected_answer'].strip()) < 2:
                    q['expected_answer'] = 'правильный ответ'
                q['options'] = None

        print(f"✅ Сгенерировано {len(filtered[:count])} вопросов")
        return {"questions": filtered[:count]}

    except requests.exceptions.Timeout:
        print(f"⏱️ Таймаут запроса к YandexGPT")
        return {"error": "Таймаут: YandexGPT не отвечает"}
    except requests.exceptions.ConnectionError:
        print(f"🌐 Ошибка соединения с YandexGPT")
        return {"error": "Нет соединения с YandexGPT API"}
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        return {"error": f"Ошибка: {str(e)}"}


def search_info_online(topic):
    """Поиск информации в Wikipedia с защитой от всех ошибок"""
    try:
        wikipedia.set_lang("ru")
        print(f"🔍 Поиск в Wikipedia: '{topic}'")

        # Пробуем поиск с защитой
        try:
            results = wikipedia.search(topic, results=3)
            print(f"📋 Найдено статей: {len(results)}")
        except Exception as e:
            print(f"⚠️ Ошибка поиска Wikipedia: {type(e).__name__}: {e}")
            results = []

        if not results:
            fallback = f"Создай образовательный тест из 5 вопросов по теме: {topic}"
            print(f"⚠️ Wikipedia: ничего не найдено, fallback")
            return fallback, None

        # Пробуем получить содержимое
        for result in results[:2]:  # Только первые 2 статьи
            try:
                page = wikipedia.page(result, auto_suggest=False)
                content = page.summary[:2000]  # Меньше текста

                if len(content.strip()) > 50:
                    print(f"✅ Найдено: {page.title} ({len(content)} симв.)")
                    return content, None
            except wikipedia.exceptions.DisambiguationError:
                print(f"⚠️ '{result}': неоднозначность - пропускаем")
                continue
            except wikipedia.exceptions.PageError:
                print(f"⚠️ '{result}': страница не найдена")
                continue
            except Exception as e:
                print(f"⚠️ Ошибка '{result}': {type(e).__name__}: {e}")
                continue

        # Fallback
        fallback = f"Создай образовательный тест из 5 вопросов по теме: {topic}"
        print(f"⚠️ Используем fallback")
        return fallback, None

    except Exception as e:
        print(f"❌ Критическая ошибка Wikipedia: {type(e).__name__}: {e}")
        return f"Создай тест из 5 вопросов по теме: {topic}", None

def login():
    data = request.get_json()
    if not data: return jsonify({"error": "Пустой запрос"}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password: return jsonify({"error": "Введите логин и пароль"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT id, username, full_name, password_hash, email, role, group_id, subject, is_active FROM users WHERE username = ?''',
            (username,))
        user = cursor.fetchone()
        if not user or not user['is_active'] or not check_password_hash(user['password_hash'], password):
            return jsonify({"error": "Неверный логин или пароль"}), 401
        cursor.execute("UPDATE users SET last_login = ? WHERE id = ?",
                       (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user['id']))
        conn.commit()
        session['user_id'] = user['id']
        session['username'] = user['username']
        session['full_name'] = user['full_name']
        session['role'] = user['role']
        session['group_id'] = user['group_id']
        print(f"✅ Вход: {username} ({user['role']})")
        return jsonify({"status": "success",
                        "user": {"id": user['id'], "username": user['username'], "full_name": user['full_name'],
                                 "role": user['role'], "group_id": user['group_id'], "subject": user['subject']}})
    except Exception as e:
        print(f"❌ Ошибка входа: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({"status": "success", "message": "Выход выполнен"})


@app.route('/api/auth/status')
def auth_status():
    if 'user_id' in session:
        return jsonify({"authenticated": True, "username": session['username'], "role": session['role'],
                        "full_name": session['full_name']})
    return jsonify({"authenticated": False})


@app.route('/api/admin/register', methods=['POST'])
@require_admin
def admin_register():
    data = request.get_json()
    if not data: return jsonify({"error": "Пустой запрос"}), 400
    username = data.get('username', '').strip()
    full_name = data.get('full_name', '').strip()
    role = data.get('role', 'student')
    password = data.get('password', '')
    email = data.get('email', '').strip()
    group_name = data.get('group') if role == 'student' else None
    subject = data.get('subject') if role == 'teacher' else None
    if len(username) < 3: return jsonify({"error": "Логин от 3 символов"}), 400
    if len(password) < 6: return jsonify({"error": "Пароль от 6 символов"}), 400
    if role not in ['student', 'teacher']: return jsonify({"error": "Неверная роль"}), 400
    if role == 'student' and not group_name: return jsonify({"error": "Группа обязательна"}), 400
    if role == 'teacher' and not subject: return jsonify({"error": "Предмет обязателен"}), 400
    if not re.match(r'^[a-zA-Z0-9_]+$', username): return jsonify({"error": "Логин: буквы, цифры, _"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        group_id = None
        if role == 'student' and group_name:
            cursor.execute("SELECT id FROM groups WHERE name = ?", (group_name,))
            g = cursor.fetchone()
            if not g:
                cursor.execute("INSERT INTO groups (name, created_at) VALUES (?, ?)",
                               (group_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                group_id = cursor.lastrowid
            else:
                group_id = g['id']
        cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
        if cursor.fetchone(): return jsonify({"error": "Пользователь существует"}), 409
        cursor.execute(
            '''INSERT INTO users (username, full_name, password_hash, email, role, group_id, subject, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (username, full_name or username, generate_password_hash(password), email or None, role, group_id, subject,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        print(f"✅ Создан: {username} ({role})")
        return jsonify({"status": "success", "message": "Пользователь создан", "user_id": cursor.lastrowid})
    except sqlite3.IntegrityError as e:
        return jsonify({"error": "Пользователь существует"}), 409
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_get_users():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT u.id, u.username, u.full_name, u.email, u.role, u.subject, g.name as group_name, u.created_at, u.is_active FROM users u LEFT JOIN groups g ON u.group_id = g.id ORDER BY u.created_at DESC''')
        return jsonify({"users": [dict(r) for r in cursor.fetchall()]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/user/<int:user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user_id):
    """Удаление пользователя (только для админа)"""
    # 🔐 Защита от удаления самого себя
    if user_id == session.get('user_id'):
        return jsonify({"error": "Нельзя удалить самого себя"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 🔍 Проверяем существование пользователя
        cursor.execute("SELECT id, username, role FROM users WHERE id = ?", (user_id,))
        user = cursor.fetchone()
        if not user:
            return jsonify({"error": "Пользователь не найден"}), 404

        # 🗑️ Удаляем связанные данные
        cursor.execute("DELETE FROM results WHERE user_id = ?", (user_id,))
        cursor.execute("DELETE FROM notifications WHERE user_id = ?", (user_id,))

        # ⚠️ group_members может не существовать - обрабатываем ошибку
        try:
            cursor.execute("DELETE FROM group_members WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError:
            pass  # Таблица не существует, пропускаем

        # 👨‍🏫 Если пользователь - учитель, удаляем его тесты
        if user['role'] == 'teacher':
            cursor.execute("SELECT id FROM tests WHERE teacher_id = ?", (user_id,))
            for t in cursor.fetchall():
                cursor.execute("DELETE FROM test_questions WHERE test_id = ?", (t['id'],))
                cursor.execute("DELETE FROM test_assignments WHERE test_id = ?", (t['id'],))
            cursor.execute("DELETE FROM tests WHERE teacher_id = ?", (user_id,))

        # ❌ Удаляем самого пользователя
        cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()

        print(f"✅ Удалён: {user['username']}")
        return jsonify({
            "status": "success",
            "message": f"Пользователь {user['username']} удалён"
        })

    except sqlite3.Error as e:
        print(f"❌ Ошибка БД при удалении: {e}")
        if conn: conn.rollback()
        return jsonify({"error": f"Ошибка базы данных: {str(e)}"}), 500
    except Exception as e:
        print(f"❌ Ошибка при удалении пользователя: {e}")
        return jsonify({"error": f"Ошибка сервера: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/admin/user/<int:user_id>', methods=['PUT'])
@require_admin
def admin_update_user(user_id):
    data = request.get_json()
    if not data: return jsonify({"error": "Пустой запрос"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        updates = []
        params = []
        if 'is_active' in data:
            updates.append("is_active = ?")
            params.append(1 if data['is_active'] else 0)
        if 'email' in data and data['email']:
            updates.append("email = ?")
            params.append(data['email'])
        if not updates: return jsonify({"error": "Нет данных для обновления"}), 400
        params.append(user_id)
        cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        if cursor.rowcount == 0: return jsonify({"error": "Пользователь не найден"}), 404
        return jsonify({"status": "success", "message": "Пользователь обновлён"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/stats', methods=['GET'])
@require_admin
def admin_stats():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        stats = {}
        cursor.execute("SELECT COUNT(*) FROM users")
        stats['total_users'] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE role='student'")
        stats['students'] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM users WHERE role='teacher'")
        stats['teachers'] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM groups")
        stats['groups'] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tests")
        stats['tests_total'] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tests WHERE status='published'")
        stats['tests_published'] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM results")
        stats['results_total'] = cursor.fetchone()[0]
        cursor.execute("SELECT AVG(score*100.0/NULLIF(total_points,0)) FROM results")
        avg = cursor.fetchone()[0]
        stats['avg_score'] = round(avg, 1) if avg else 0
        cursor.execute("SELECT COUNT(*) FROM notifications WHERE is_read=0")
        stats['unread_notifications'] = cursor.fetchone()[0]
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/backup', methods=['GET'])
@require_admin
def admin_backup():
    conn = None
    try:
        conn = get_db_connection()
        backup = {"timestamp": datetime.now().isoformat(), "tables": {}}
        for table in ['users', 'groups', 'tests', 'test_questions', 'test_assignments', 'notifications', 'results']:
            cursor = conn.cursor()
            cursor.execute(f"SELECT * FROM {table}")
            backup["tables"][table] = [dict(r) for r in cursor.fetchall()]
        output = io.BytesIO(json.dumps(backup, ensure_ascii=False, indent=2).encode('utf-8'))
        output.seek(0)
        ts = datetime.now().strftime('%Y%m%d_%H%M')
        return send_file(output, mimetype='application/json', download_name=f'simtestin_backup_{ts}.json',
                         as_attachment=True)
    except Exception as e:
        return jsonify({"error": f"Ошибка бэкапа: {str(e)}"}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/export', methods=['GET'])
@require_admin
def admin_export():
    @app.route('/api/admin/export', methods=['GET'])
    @require_admin
    def admin_export():
        fmt = request.args.get('format', 'xlsx').lower()
        if fmt not in ['xlsx', 'txt', 'csv']:
            return jsonify({"error": "Формат: xlsx, txt, csv"}), 400

        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT r.id, r.student_name, t.title as test, r.score, r.total_points, 
                          ROUND(r.score*100.0/NULLIF(r.total_points,0),1) as percent, 
                          r.date, u.username, u.full_name, g.name as group_name 
                   FROM results r 
                   JOIN tests t ON r.test_id=t.id 
                   JOIN users u ON r.user_id=u.id 
                   LEFT JOIN groups g ON u.group_id=g.id 
                   ORDER BY r.date DESC''')
            rows = [dict(r) for r in cursor.fetchall()]

            if not rows:
                return jsonify({"error": "Нет данных"}), 404

            ts = datetime.now().strftime('%Y%m%d_%H%M')

            if fmt == 'csv':
                import csv
                output = io.StringIO()
                if rows:
                    writer = csv.DictWriter(output, fieldnames=rows[0].keys(), delimiter=';')
                    writer.writeheader()
                    writer.writerows(rows)
                output_bytes = io.BytesIO(output.getvalue().encode('utf-8-sig'))
                output_bytes.seek(0)
                return send_file(output_bytes, mimetype='text/csv',
                                 download_name=f'simtestin_{ts}.csv', as_attachment=True)

            elif fmt == 'xlsx':
                from openpyxl import Workbook
                wb = Workbook()
                ws = wb.active
                ws.title = 'Results'

                # Заголовки
                if rows:
                    headers = list(rows[0].keys())
                    ws.append(headers)
                    # Данные
                    for row in rows:
                        ws.append([row[h] for h in headers])

                output = io.BytesIO()
                wb.save(output)
                output.seek(0)
                return send_file(output,
                                 mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                                 download_name=f'simtestin_{ts}.xlsx', as_attachment=True)

            else:  # txt
                output = io.StringIO()
                output.write(f"SimTestin Export | {datetime.now().strftime('%Y-%m-%d %H:%M')}\n" + "=" * 100 + "\n\n")
                for r in rows:
                    output.write(
                        f"👤 {r['full_name'] or r['student_name']} ({r['username']})\n"
                        f"📚 {r['test']}\n"
                        f"✅ {r['score']}/{r['total_points']} ({r['percent']}%)\n"
                        f"📅 {r['date']} | 👥 {r['group_name'] or '—'}\n"
                        f"{'-' * 100}\n")
                output_bytes = io.BytesIO(output.getvalue().encode('utf-8'))
                output_bytes.seek(0)
                return send_file(output_bytes, mimetype='text/plain',
                                 download_name=f'simtestin_{ts}.txt', as_attachment=True)

        except Exception as e:
            return jsonify({"error": f"Ошибка экспорта: {str(e)}"}), 500
        finally:
            if conn:
                conn.close()


@app.route('/api/admin/results', methods=['GET'])
@require_admin
def admin_get_results():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT r.id, r.student_name, r.test_id, t.title as test_title, r.score, r.total_points, r.date, u.username, u.full_name as student_full, g.name as group_name FROM results r JOIN tests t ON r.test_id = t.id JOIN users u ON r.user_id = u.id LEFT JOIN groups g ON u.group_id = g.id ORDER BY r.date DESC''')
        return jsonify({"results": [dict(r) for r in cursor.fetchall()]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/delete_result/<int:result_id>', methods=['DELETE'])
@require_admin
def admin_delete_result(result_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM results WHERE id = ?", (result_id,))
        conn.commit()
        return jsonify({"status": "success", "message": "Результат удалён"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/admin/clear_results', methods=['DELETE'])
@require_admin
def admin_clear_results():
    """Очистка всех результатов тестов (только для админа)"""
    confirm_header = request.headers.get('X-Confirm', '').strip().lower()
    if confirm_header != 'true':
        print(f"⚠️ Отказ в очистке: X-Confirm = '{request.headers.get('X-Confirm')}'")
        return jsonify({"error": "Требуется заголовок: X-Confirm: true"}), 400

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='results'")
        if not cursor.fetchone():
            return jsonify({"error": "Таблица results не существует"}), 500

        cursor.execute("SELECT COUNT(*) FROM results")
        count = cursor.fetchone()[0]

        cursor.execute("DELETE FROM results")
        conn.commit()

        print(f"✅ Админ {session.get('username')} очистил {count} результатов")
        return jsonify({
            "status": "success",
            "deleted": count,
            "message": f"Удалено записей: {count}"
        })

    except sqlite3.Error as e:
        print(f"❌ Ошибка БД при очистке: {e}")
        if conn: conn.rollback()
        return jsonify({"error": f"Ошибка базы данных: {str(e)}"}), 500
    except Exception as e:
        print(f"❌ Ошибка при очистке результатов: {e}")
        return jsonify({"error": f"Ошибка сервера: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/admin/groups', methods=['GET'])
@require_admin
def admin_get_groups():
    """Получение списка групп для администратора"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='groups'")
        if not cursor.fetchone():
            return jsonify({"error": "Таблица groups не существует"}), 500

        cursor.execute('''
            SELECT g.id, g.name, g.description, g.teacher_id, g.created_at,
                   u.username as teacher_name
            FROM groups g
            LEFT JOIN users u ON g.teacher_id = u.id
            ORDER BY g.name
        ''')

        groups = []
        for row in cursor.fetchall():
            groups.append({
                "id": row["id"],
                "name": row["name"],
                "description": row["description"] or "",
                "teacher_id": row["teacher_id"],
                "teacher_name": row["teacher_name"] or "—",
                "created_at": row["created_at"]
            })

        print(f"✅ Загружено групп для админа: {len(groups)}")
        return jsonify({"groups": groups})

    except Exception as e:
        print(f"❌ Ошибка загрузки групп: {e}")
        return jsonify({"error": f"Ошибка сервера: {str(e)}"}), 500
    finally:
        if conn:
            conn.close()


@app.route('/api/teacher/groups', methods=['GET', 'POST'])
@login_required
@role_required('teacher', 'admin')
def teacher_groups():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if request.method == 'POST':
            name = request.json.get('name', '').strip()
            if not name: return jsonify({"error": "Название обязательно"}), 400
            cursor.execute("INSERT INTO groups (name, teacher_id, created_at) VALUES (?, ?, ?)",
                           (name, session['user_id'], datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            return jsonify({"status": "success", "group_id": cursor.lastrowid})
        cursor.execute("SELECT * FROM groups ORDER BY name")
        return jsonify({"groups": [dict(r) for r in cursor.fetchall()]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/teacher/tests', methods=['GET', 'POST'])
@login_required
@role_required('teacher', 'admin')
def teacher_tests():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if request.method == 'POST':
            data = request.json
            title = data.get('title', '').strip()
            topic = data.get('topic', '').strip()
            if not title or not topic: return jsonify({"error": "Название и тема обязательны"}), 400
            questions = []
            if data.get('use_ai'):
                content, err = search_info_online(topic)
                if err: return jsonify({"error": f"Поиск: {err}"}), 404
                ai = generate_test_via_ai(content, data.get('difficulty', 'средняя'), data.get('count', 5),
                                          data.get('question_types'))
                if "error" in ai: return jsonify(ai), 500
                questions = ai["questions"]
                print(f"✅ Сгенерировано {len(questions)} вопросов типов: {set(q['type'] for q in questions)}")
            else:
                questions = data.get('questions', [])
            cursor.execute(
                '''INSERT INTO tests (title, topic, difficulty, teacher_id, question_count, question_types, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)''',
                (title, topic, data.get('difficulty'), session['user_id'], len(questions),
                 json.dumps(data.get('question_types')), datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            test_id = cursor.lastrowid
            for i, q in enumerate(questions):
                correct_answer = None
                if q.get('type') == 'single' and q.get('correct_index') is not None:
                    correct_answer = int(q['correct_index'])
                cursor.execute(
                    '''INSERT INTO test_questions (test_id, question_text, question_type, options, correct_answer, correct_answers, expected_answer, points, question_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (test_id, q['question'], q.get('type', 'single'),
                     json.dumps(q.get('options'), ensure_ascii=False) if q.get('options') else None,
                     correct_answer,
                     json.dumps(q.get('correct_answers')) if q.get('type') == 'multiple' else None,
                     q.get('expected_answer') if q.get('type') == 'text' else None, q.get('points', 1), i))
            conn.commit()
            return jsonify({"status": "success", "test_id": test_id})
        cursor.execute("SELECT * FROM tests WHERE teacher_id = ? ORDER BY created_at DESC", (session['user_id'],))
        return jsonify({"tests": [dict(r) for r in cursor.fetchall()]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/teacher/tests/<int:test_id>', methods=['GET', 'PUT', 'DELETE'])
@login_required
@role_required('teacher', 'admin')
def teacher_test_detail(test_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tests WHERE id = ? AND teacher_id = ?", (test_id, session['user_id']))
        test = cursor.fetchone()
        if not test: return jsonify({"error": "Тест не найден"}), 404
        if request.method == 'GET':
            cursor.execute("SELECT * FROM test_questions WHERE test_id = ? ORDER BY question_order", (test_id,))
            questions = []
            for r in cursor.fetchall():
                q = dict(r)
                if q['options']: q['options'] = json.loads(q['options'])
                if q['correct_answers']: q['correct_answers'] = json.loads(q['correct_answers'])
                if q['question_type'] == 'single' and q['correct_answer'] is not None:
                    try:
                        q['correct_answer'] = int(q['correct_answer'])
                    except:
                        q['correct_answer'] = 0
                questions.append(q)
            return jsonify({"test": dict(test), "questions": questions})
        elif request.method == 'PUT':
            data = request.json
            if data.get('title'): cursor.execute("UPDATE tests SET title = ? WHERE id = ?", (data['title'], test_id))
            if data.get('questions'):
                for q in data['questions']:
                    if q.get('id'):
                        correct_answer = None
                        if q.get('question_type') == 'single' and q.get('correct_answer') is not None:
                            correct_answer = int(q['correct_answer'])
                        cursor.execute(
                            '''UPDATE test_questions SET question_text = ?, question_type = ?, options = ?, correct_answer = ?, correct_answers = ?, expected_answer = ?, points = ? WHERE id = ?''',
                            (q['question_text'], q['question_type'],
                             json.dumps(q.get('options'), ensure_ascii=False) if q.get('options') else None,
                             correct_answer,
                             json.dumps(q.get('correct_answers')) if q.get('question_type') == 'multiple' else None,
                             q.get('expected_answer'), q.get('points', 1), q['id']))
            conn.commit()
            return jsonify({"status": "success"})
        elif request.method == 'DELETE':
            cursor.execute("DELETE FROM test_questions WHERE test_id = ?", (test_id,))
            cursor.execute("DELETE FROM tests WHERE id = ?", (test_id,))
            conn.commit()
            return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/teacher/tests/<int:test_id>/publish', methods=['POST'])
@login_required
@role_required('teacher', 'admin')
def publish_test(test_id):
    data = request.json
    group_id = data.get('group_id')
    if not group_id: return jsonify({"error": "Не выбрана группа"}), 400
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM tests WHERE id = ? AND teacher_id = ?", (test_id, session['user_id']))
        test = cursor.fetchone()
        if not test: return jsonify({"error": "Тест не найден"}), 404
        cursor.execute("UPDATE tests SET status = 'published', published_at = ? WHERE id = ?",
                       (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), test_id))
        cursor.execute('''INSERT INTO test_assignments (test_id, group_id, assigned_at) VALUES (?, ?, ?)''',
                       (test_id, group_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        cursor.execute("SELECT id FROM users WHERE group_id = ? AND role = 'student'", (group_id,))
        for student in cursor.fetchall():
            cursor.execute(
                '''INSERT INTO notifications (user_id, title, message, type, related_id, created_at) VALUES (?, ?, ?, ?, ?, ?)''',
                (student['id'], '📝 Новый тест', f'Вам назначен: "{test["title"]}"', 'test_assigned', test_id,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/teacher/results', methods=['GET'])
@login_required
@role_required('teacher', 'admin')
def teacher_get_results():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if session['role'] == 'teacher':
            cursor.execute('''
                SELECT r.id, r.student_name, r.test_id, t.title as test_title, 
                       r.score, r.total_points, r.date, u.username, 
                       u.full_name as student_full, g.name as group_name
                FROM results r 
                JOIN tests t ON r.test_id = t.id
                JOIN users u ON r.user_id = u.id
                LEFT JOIN groups g ON u.group_id = g.id
                WHERE t.teacher_id = ?
                ORDER BY r.date DESC
            ''', (session['user_id'],))
        else:
            cursor.execute('''
                SELECT r.id, r.student_name, r.test_id, t.title as test_title, 
                       r.score, r.total_points, r.date, u.username, 
                       u.full_name as student_full, g.name as group_name
                FROM results r 
                JOIN tests t ON r.test_id = t.id
                JOIN users u ON r.user_id = u.id
                LEFT JOIN groups g ON u.group_id = g.id
                ORDER BY r.date DESC
            ''')
        return jsonify({"results": [dict(r) for r in cursor.fetchall()]})
    except Exception as e:
        print(f"❌ Ошибка загрузки результатов: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/teacher/test/<int:test_id>/answers', methods=['GET'])
@login_required
@role_required('teacher', 'admin')
def teacher_view_answers(test_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT teacher_id FROM tests WHERE id = ?", (test_id,))
        test = cursor.fetchone()
        if not test or (test['teacher_id'] != session['user_id'] and session['role'] != 'admin'):
            return jsonify({"error": "Доступ запрещён"}), 403
        cursor.execute(
            '''SELECT r.id, r.student_name, r.score, r.total_points, r.date, u.username, r.answers_data FROM results r JOIN users u ON r.user_id = u.id WHERE r.test_id = ? ORDER BY r.date DESC''',
            (test_id,))
        results = []
        for row in cursor.fetchall():
            r = dict(row)
            if r['answers_data']:
                try:
                    r['answers'] = json.loads(r['answers_data'])
                except:
                    r['answers'] = []
            else:
                r['answers'] = []
            results.append(r)
        return jsonify({"test_id": test_id, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/student/dashboard', methods=['GET'])
@login_required
@role_required('student')
def student_dashboard():
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT t.*, ta.due_date, g.name as group_name FROM tests t JOIN test_assignments ta ON t.id = ta.test_id JOIN groups g ON ta.group_id = g.id WHERE g.id = ? AND t.status = 'published' AND ta.is_active = 1 ORDER BY ta.assigned_at DESC''',
            (session['group_id'],))
        tests = []
        for row in cursor.fetchall():
            cursor.execute("SELECT id FROM results WHERE user_id = ? AND test_id = ?", (session['user_id'], row['id']))
            tests.append({**dict(row), "already_taken": cursor.fetchone() is not None})
        cursor.execute(
            "SELECT * FROM notifications WHERE user_id = ? AND is_read = 0 ORDER BY created_at DESC LIMIT 10",
            (session['user_id'],))
        return jsonify({"tests": tests, "notifications": [dict(r) for r in cursor.fetchall()]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/student/tests/<int:test_id>/start', methods=['POST'])
@login_required
@role_required('student')
def student_start_test(test_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT 1 FROM test_assignments ta JOIN groups g ON ta.group_id = g.id JOIN users u ON u.group_id = g.id WHERE ta.test_id = ? AND u.id = ? AND ta.is_active = 1''',
            (test_id, session['user_id']))
        if not cursor.fetchone(): return jsonify({"error": "Тест недоступен"}), 403
        cursor.execute("SELECT id FROM results WHERE user_id = ? AND test_id = ?", (session['user_id'], test_id))
        if cursor.fetchone(): return jsonify({"error": "Вы уже прошли этот тест"}), 409
        cursor.execute(
            '''SELECT id, question_text, question_type, options, points, question_order FROM test_questions WHERE test_id = ? ORDER BY question_order''',
            (test_id,))
        questions = []
        for r in cursor.fetchall():
            q = {"id": r['id'], "question": r['question_text'], "type": r['question_type'], "points": r['points']}
            if r['question_type'] in ['single', 'multiple'] and r['options']: q['options'] = json.loads(r['options'])
            questions.append(q)
        random.shuffle(questions)
        for q in questions:
            if 'options' in q and len(q['options']) > 1:
                opts = list(enumerate(q['options']))
                random.shuffle(opts)
                q['options'] = [o[1] for o in opts]
        return jsonify({"questions": questions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/student/tests/<int:test_id>/submit', methods=['POST'])
@login_required
@role_required('student')
def student_submit_test(test_id):
    data = request.json
    answers = data.get('answers', [])
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT 1 FROM test_assignments ta JOIN groups g ON ta.group_id = g.id JOIN users u ON u.group_id = g.id WHERE ta.test_id = ? AND u.id = ?''',
            (test_id, session['user_id']))
        if not cursor.fetchone(): return jsonify({"error": "Тест недоступен"}), 403
        qids = [a['question_id'] for a in answers]
        if not qids: return jsonify({"error": "Нет ответов"}), 400
        placeholders = ','.join('?' * len(qids))
        cursor.execute(
            f'''SELECT id, question_type, correct_answer, correct_answers, expected_answer, points FROM test_questions WHERE id IN ({placeholders})''',
            qids)
        questions_map = {r['id']: dict(r) for r in cursor.fetchall()}
        score, total_points, user_answers = 0, 0, []
        for ans in answers:
            q = questions_map.get(ans['question_id'])
            if not q: continue
            total_points += q['points']
            is_correct = False
            if q['question_type'] == 'single':
                user_ans = int(ans.get('user_answer')) if ans.get('user_answer') is not None else -1
                correct_ans = int(q['correct_answer']) if q['correct_answer'] is not None else -1
                is_correct = user_ans == correct_ans
            elif q['question_type'] == 'multiple':
                correct = set(int(i) for i in (json.loads(q['correct_answers']) or []))
                user = set(int(i) for i in (ans.get('user_answer') or []))
                is_correct = correct == user
            elif q['question_type'] == 'text':
                expected = (q['expected_answer'] or '').lower().strip()
                user_ans = (ans.get('user_answer') or '').lower().strip()
                is_correct = expected == user_ans or expected in user_ans or user_ans in expected
            if is_correct: score += q['points']
            user_answers.append(
                {"question_id": ans['question_id'], "user_answer": ans.get('user_answer'), "is_correct": is_correct,
                 "points_earned": q['points'] if is_correct else 0})
        cursor.execute("SELECT title, topic FROM tests WHERE id = ?", (test_id,))
        test = cursor.fetchone()
        cursor.execute(
            '''INSERT INTO results (user_id, test_id, student_name, score, total_points, date, answers_data) VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (session['user_id'], test_id, session['full_name'] or session['username'], score, total_points,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), json.dumps(user_answers, ensure_ascii=False)))
        cursor.execute("SELECT teacher_id FROM tests WHERE id = ?", (test_id,))
        teacher = cursor.fetchone()
        if teacher:
            cursor.execute(
                '''INSERT INTO notifications (user_id, title, message, type, related_id, created_at) VALUES (?, ?, ?, ?, ?, ?)''',
                (teacher['teacher_id'], '✅ Тест сдан',
                 f'{session["full_name"] or session["username"]} завершил "{test["title"]}"', 'result_submitted',
                 test_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return jsonify({"status": "success", "message": "Тест отправлен. Результат доступен учителю."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/student/notifications/<int:notif_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notif_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE notifications SET is_read = 1 WHERE id = ? AND user_id = ?",
                       (notif_id, session['user_id']))
        conn.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        if conn: conn.close()


@app.route('/api/debug/auth')
def debug_auth():
    pwd = request.args.get('admin_password') or request.headers.get('X-Admin-Password')
    return jsonify({"ADMIN_PASSWORD_loaded": bool(ADMIN_PASSWORD),
                    "access_granted": bool(ADMIN_PASSWORD and pwd == ADMIN_PASSWORD),
                    "timestamp": datetime.now().isoformat()})


init_db()

# === ДИАГНОСТИКА ПРИ СТАРТЕ ===
print("\n" + "═" * 70)
print("🎓 SimTestin — Запуск")
print("═" * 70)
print(f"🔐 ADMIN_PASSWORD: {'✅ задан' if ADMIN_PASSWORD else '❌ НЕ ЗАДАН!'}")
print(f"📁 YANDEX_FOLDER_ID: {FOLDER_ID if FOLDER_ID else '❌ НЕ ЗАДАН!'}")
print(f"🔑 YANDEX_API_KEY: {'✅ ' + API_KEY[:10] + '...' if API_KEY else '❌ Не задан'}")
print(f"🔑 YANDEX_IAM_TOKEN: {'✅ задан' if IAM_TOKEN else '❌ Не задан'}")
print(f"🗄️ DATABASE_URL: {'✅ PostgreSQL' if os.getenv('DATABASE_URL') else '📁 SQLite'}")
print("═" * 70 + "\n")

if __name__ == '__main__':
    debug_mode = os.getenv('FLASK_DEBUG', 'true').lower() == 'true'
    host = os.getenv('FLASK_HOST', '0.0.0.0')
    port = int(os.getenv('PORT', 5000))

    local_url = f"http://127.0.0.1:{port}" if host in ['0.0.0.0', '127.0.0.1'] else f"http://{host}:{port}"
    print(f"🔗 Локальная версия: {local_url}")
    print(f"🌐 Режим: {'🔧 ОТЛАДКА' if debug_mode else '🚀 ПРОДАКШЕН'}")

    if debug_mode:
        app.run(debug=True, host=host, port=port, threaded=True)