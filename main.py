import os
from fastapi import FastAPI, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from authlib.integrations.starlette_client import OAuth
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
import sqlite3

from database import get_db, init_db

# Initialize database on startup
init_db()

app = FastAPI(title="AI Checklist Webapp")

# Secure session middleware (Secret key should ideally come from an env variable)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "super-secret-homelab-key"))

# OAuth Setup
oauth = OAuth()
oauth.register(
    name='google',
    client_id=os.getenv('GOOGLE_CLIENT_ID', ''),
    client_secret=os.getenv('GOOGLE_CLIENT_SECRET', ''),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'}
)

templates = Jinja2Templates(directory="templates")

# --- Pydantic Models ---
class ListCreate(BaseModel):
    title: str
    is_resettable: bool = False

class TaskCreate(BaseModel):
    title: str

class ShareRequest(BaseModel):
    email: str

class ReorderRequest(BaseModel):
    task_ids: list[int]

# --- Helper: Get Current User from Session ---
def get_current_user(request: Request):
    user = request.session.get('user')
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user

# --- Auth Routes ---
@app.route('/auth/login')
async def login(request: Request):
    redirect_uri = request.url_for('auth_callback')
    return await oauth.google.authorize_redirect(request, redirect_uri)

@app.route('/auth/callback')
async def auth_callback(request: Request):
    token = await oauth.google.authorize_access_token(request)
    user_info = token.get('userinfo')
    if not user_info:
        raise HTTPException(status_code=400, detail="Google authentication failed")
    
    email = user_info['email']
    name = user_info.get('name', email)

    # Upsert user into database
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()
    
    if not row:
        cursor.execute("INSERT INTO users (email, name) VALUES (?, ?)", (email, name))
        conn.commit()
        user_id = cursor.lastrowid
    else:
        user_id = row[0]
    conn.close()

    # Save session data
    request.session['user'] = {
        'id': user_id,
        'email': email,
        'name': name
    }
    return RedirectResponse(url='/', status_code=303)

@app.get('/auth/logout')
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url='/', status_code=303)

# --- Frontend Route ---
@app.get('/')
async def index(request: Request):
    user = request.session.get('user')
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

# --- API: Lists ---
@app.get('/api/lists')
async def get_lists(request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Fetch owned lists + shared lists
    cursor.execute("""
        SELECT DISTINCT l.id, l.title, l.user_id, u.name as owner_name 
        FROM lists l
        JOIN users u ON l.user_id = u.id
        LEFT JOIN list_shares ls ON l.id = ls.list_id
        WHERE l.user_id = ? OR ls.user_id = ?
        ORDER BY l.created_at DESC
    """, (user['id'], user['id']))
    
    lists = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return lists

@app.post('/api/lists')
async def create_list(payload: ListCreate, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO lists (user_id, title, is_resettable) VALUES (?, ?, ?)", (user['id'], payload.title, 1 if payload.is_resettable else 0))
    conn.commit()
    list_id = cursor.lastrowid
    conn.close()
    return {"id": list_id, "title": payload.title, "is_resettable": payload.is_resettable, "user_id": user['id']}

@app.delete('/api/lists/{list_id}')
async def delete_list(list_id: int, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    # Ensure user owns the list
    cursor.execute("DELETE FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    conn.commit()
    conn.close()
    return {"success": True}

# --- API: Tasks ---
@app.get('/api/lists/{list_id}/tasks')
async def get_tasks(list_id: int, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Verify access (owner or shared)
    cursor.execute("""
        SELECT 1 FROM lists l 
        LEFT JOIN list_shares ls ON l.id = ls.list_id 
        WHERE l.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (list_id, user['id'], user['id']))
    
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("SELECT id, title, status, position FROM tasks WHERE list_id = ? ORDER BY position ASC, id DESC", (list_id,))
    tasks = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return tasks

@app.post('/api/lists/{list_id}/tasks')
async def create_task(list_id: int, payload: TaskCreate, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    
    # Verify access
    cursor.execute("""
        SELECT 1 FROM lists l 
        LEFT JOIN list_shares ls ON l.id = ls.list_id 
        WHERE l.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (list_id, user['id'], user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("INSERT INTO tasks (list_id, title) VALUES (?, ?)", (list_id, payload.title))
    conn.commit()
    task_id = cursor.lastrowid
    conn.close()
    return {"id": task_id, "title": payload.title, "status": "pending"}

@app.patch('/api/tasks/{task_id}')
async def update_task_status(task_id: int, request: Request):
    user = get_current_user(request)
    data = await request.json()
    new_status = data.get('status')
    
    if new_status not in ['pending', 'completed']:
        raise HTTPException(status_code=400, detail="Invalid status")
        
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    
    # Verify user has access to the list containing this task via ownership or share
    cursor.execute("""
        SELECT 1 FROM tasks t
        JOIN lists l ON t.list_id = l.id
        LEFT JOIN list_shares ls ON l.id = ls.list_id
        WHERE t.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (task_id, user['id'], user['id']))
    
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    conn.commit()
    conn.close()
    return {"success": True}

@app.delete('/api/tasks/{task_id}')
async def delete_task(task_id: int, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 1 FROM tasks t
        JOIN lists l ON t.list_id = l.id
        LEFT JOIN list_shares ls ON l.id = ls.list_id
        WHERE t.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (task_id, user['id'], user['id']))
    
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")

    cursor.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post('/api/lists/{list_id}/reset')
async def reset_list(list_id: int, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    
    # Verify owner access for resetting
    cursor.execute("SELECT 1 FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Only list owner can reset daily tasks")

    cursor.execute("UPDATE tasks SET status = 'pending' WHERE list_id = ?", (list_id,))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post('/api/lists/{list_id}/share')
async def share_list(list_id: int, payload: ShareRequest, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    
    # Verify owner access
    cursor.execute("SELECT 1 FROM lists WHERE id = ? AND user_id = ?", (list_id, user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Only list owner can share this list")
    
    # Find user by email (they must have logged in at least once)
    cursor.execute("SELECT id FROM users WHERE email = ?", (payload.email,))
    target_user = cursor.fetchone()
    if not target_user:
        conn.close()
        raise HTTPException(status_code=404, detail="User not found. They must log into the app first.")
    
    target_user_id = target_user[0]
    cursor.execute("INSERT OR IGNORE INTO list_shares (list_id, user_id) VALUES (?, ?)", (list_id, target_user_id))
    conn.commit()
    conn.close()
    return {"success": True}

@app.post('/api/lists/{list_id}/reorder')
async def reorder_tasks(list_id: int, payload: ReorderRequest, request: Request):
    user = get_current_user(request)
    conn = sqlite3.connect("checklist.db")
    cursor = conn.cursor()
    
    # Verify access (owner or shared)
    cursor.execute("""
        SELECT 1 FROM lists l 
        LEFT JOIN list_shares ls ON l.id = ls.list_id 
        WHERE l.id = ? AND (l.user_id = ? OR ls.user_id = ?)
    """, (list_id, user['id'], user['id']))
    if not cursor.fetchone():
        conn.close()
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Update task positions based on array index
    for index, task_id in enumerate(payload.task_ids):
        cursor.execute("UPDATE tasks SET position = ? WHERE id = ? AND list_id = ?", (index, task_id, list_id))
    conn.commit()
    conn.close()
    return {"success": True}
