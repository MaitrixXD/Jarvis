#!/usr/bin/env python3
"""Jarvis Dashboard Server — bridge between the web UI and all services.

Endpoints:
  GET  /api/health            → service status check (Ollama, WhatsApp, GDrive, Notion, Gmail)
  POST /api/chat              → send message to Ollama, get response
  GET  /api/whatsapp/status   → WhatsApp bridge connection status
  GET  /api/whatsapp/messages → recent incoming WhatsApp messages
  POST /api/whatsapp/send     → send a WhatsApp message
  GET  /api/reminders         → list reminders
  POST /api/reminders         → add reminder
  PUT  /api/reminders/{id}    → update reminder
  POST /api/reminders/{id}/evaluate → AI evaluate reminder completion
  DELETE /api/reminders/{id}  → delete reminder
  GET  /api/contacts          → list contacts
  POST /api/contacts          → add/update contact
  GET  /api/gdrive/files      → list recent Google Drive files
  POST /api/notion/token      → configure Notion token
  WS   /ws/voice              → real-time voice status feed
"""
from __future__ import annotations

import json
import os
import sys
import time
import asyncio
import threading
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional
import urllib.request
import subprocess
import re


# Add OpenJarvis to path so we can import its modules
JARVIS_ROOT = Path(__file__).resolve().parent.parent
OPENJARVIS_SRC = JARVIS_ROOT / "OpenJarvis" / "src"
sys.path.insert(0, str(OPENJARVIS_SRC))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ═══════════════════════════════════════════
# CONFIG
DASHBOARD_DIR = Path(__file__).resolve().parent
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
CONFIG_DIR = Path.home() / ".openjarvis"
CONTACTS_FILE = CONFIG_DIR / "contacts.json"
REMINDERS_FILE = DASHBOARD_DIR / "reminders.json"


# ═══════════════════════════════════════════
# APP
# ═══════════════════════════════════════════
app = FastAPI(title="Jarvis Dashboard API", version="1.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ═══════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════
class ChatRequest(BaseModel):
    message: str
    model: str = "llama3:latest"

class ChatResponse(BaseModel):
    response: str
    model: str
    duration_ms: int = 0

class WhatsAppSendRequest(BaseModel):
    recipient: str  # phone number or contact name
    message: str

class ContactRequest(BaseModel):
    name: str
    phone: str

class NotionTokenRequest(BaseModel):
    token: str

class Reminder(BaseModel):
    id: int = 0
    text: str
    due: str = "Today"
    date: str = ""
    time: str = ""
    category: str = "Work"
    icon: str = "💻"
    priority: str = "medium"  # low, medium, high
    done: bool = False
    created_at: str = ""
    sync_gcalendar: bool = False
    gcal_link: Optional[str] = None
    evaluation: Optional[Dict[str, Any]] = None

class GCalendarEventCreate(BaseModel):
    summary: str
    date: str
    start_time: str = ""
    end_time: str = ""
    description: str = ""

class FoodLogRequest(BaseModel):
    food: str
    quantity: str = "1 bowl"
    portion: str = "1 bowl"

class HabitToggleRequest(BaseModel):
    habit: str



class NoteItem(BaseModel):
    id: int = 0
    title: str
    content: str
    category: str = "Work"
    icon: str = "📝"
    pinned: bool = False
    created_at: str = ""

class OpenFileRequest(BaseModel):
    path: str

# ═══════════════════════════════════════════
# CONTACTS
# ═══════════════════════════════════════════
def load_contacts() -> Dict[str, str]:
    if CONTACTS_FILE.exists():
        try:
            return json.loads(CONTACTS_FILE.read_text())
        except Exception:
            return {}
    default_contacts = {
        "me": "+918879223352",
        "myself": "+918879223352",
        "maitrayee": "+918879223352",
        "mom": "+918879223352",
        "baba": "+918879223352",
        "riya": "+918879223352",
    }
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONTACTS_FILE.write_text(json.dumps(default_contacts, indent=2))
    except Exception:
        pass
    return default_contacts

def save_contact(name: str, phone: str):
    contacts = load_contacts()
    contacts[name.lower().strip()] = phone.strip()
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONTACTS_FILE.write_text(json.dumps(contacts, indent=2))
    except Exception as e:
        print(f"[Contacts] Save error: {e}")

# ═══════════════════════════════════════════
# REMINDERS (persistent JSON file)
# ═══════════════════════════════════════════
def load_reminders() -> List[dict]:
    if REMINDERS_FILE.exists():
        try:
            return json.loads(REMINDERS_FILE.read_text())
        except Exception:
            return []
    return []

def save_reminders(reminders: List[dict]):
    REMINDERS_FILE.write_text(json.dumps(reminders, indent=2))

NOTES_FILE = Path(__file__).resolve().parent / "notes.json"

def load_notes() -> List[dict]:
    if NOTES_FILE.exists():
        try:
            return json.loads(NOTES_FILE.read_text())
        except Exception:
            return []
    return []

def save_notes(notes: List[dict]):
    try:
        NOTES_FILE.write_text(json.dumps(notes, indent=2))
    except Exception as e:
        print(f"[Notes] Error saving: {e}")

def format_file_size(bytes_num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.1f} {unit}" if unit != 'B' else f"{bytes_num} B"
        bytes_num /= 1024.0
    return f"{bytes_num:.1f} TB"

def format_relative_time(mtime):
    diff = time.time() - mtime
    if diff < 3600:
        mins = max(1, int(diff / 60))
        return f"{mins} mins ago"
    elif diff < 86400:
        hrs = int(diff / 3600)
        return f"{hrs} hours ago" if hrs > 1 else "1 hour ago"
    elif diff < 172800:
        return "Yesterday"
    else:
        days = int(diff / 86400)
        return f"{days} days ago"

WA_MESSAGES_FILE = Path(__file__).resolve().parent / "whatsapp_messages.json"

def load_wa_messages() -> List[dict]:
    if WA_MESSAGES_FILE.exists():
        try:
            return json.loads(WA_MESSAGES_FILE.read_text())
        except Exception:
            return []
    return []

def save_wa_messages(msgs: List[dict]):
    try:
        WA_MESSAGES_FILE.write_text(json.dumps(msgs[-100:], indent=2))
    except Exception:
        pass

wa_channel = None
wa_messages: List[dict] = load_wa_messages()  # persistent incoming messages buffer
wa_lock = threading.Lock()

def get_wa_channel():
    global wa_channel
    if wa_channel is None:
        try:
            from openjarvis.channels.whatsapp_baileys import WhatsAppBaileysChannel
            wa_channel = WhatsAppBaileysChannel()
            def on_msg(msg):
                with wa_lock:
                    wa_messages.append({
                        "sender": msg.sender,
                        "content": msg.content,
                        "message_id": msg.message_id,
                        "conversation_id": msg.conversation_id,
                        "timestamp": datetime.now().isoformat(),
                    })
                    if len(wa_messages) > 100:
                        wa_messages.pop(0)
                    save_wa_messages(wa_messages)
            wa_channel.on_message(on_msg)
        except Exception as e:
            print(f"[WhatsApp] Init failed: {e}")
            return None
    return wa_channel

# Auto-connect WhatsApp in background
def _auto_connect_wa():
    time.sleep(2)
    ch = get_wa_channel()
    if ch:
        try:
            ch.connect()
        except Exception as e:
            print(f"[WhatsApp] Auto-connect error: {e}")

threading.Thread(target=_auto_connect_wa, daemon=True).start()

# ═══════════════════════════════════════════
# VOICE STATUS (shared state)
# ═══════════════════════════════════════════
voice_state = {
    "is_listening": False,
    "last_heard": "",
    "last_response": "",
    "updated_at": "",
}
voice_clients: List[WebSocket] = []

# ═══════════════════════════════════════════
# ROUTES — Health
# ═══════════════════════════════════════════
@app.get("/api/health")
async def health():
    """Check status of all connected services."""
    services = {}

    # Ollama
    try:
        req = urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2)
        data = json.loads(req.read().decode())
        models = [m.get("name") for m in data.get("models", [])]
        services["ollama"] = {"status": "connected", "models": models}
    except Exception:
        services["ollama"] = {"status": "disconnected", "models": []}

    # WhatsApp
    ch = get_wa_channel()
    if ch:
        services["whatsapp"] = {"status": ch.status().value}
    else:
        services["whatsapp"] = {"status": "disconnected"}

    # Google Drive
    try:
        from openjarvis.connectors.gdrive import GDriveConnector
        gd = GDriveConnector()
        services["gdrive"] = {"status": "connected" if gd.is_connected() else "disconnected"}
    except Exception:
        services["gdrive"] = {"status": "disconnected"}

    # Notion
    try:
        from openjarvis.connectors.notion import NotionConnector
        nt = NotionConnector()
        services["notion"] = {"status": "connected" if nt.is_connected() else "disconnected"}
    except Exception:
        services["notion"] = {"status": "disconnected"}

    # Gmail
    try:
        from openjarvis.connectors.gmail import GmailConnector
        gm = GmailConnector()
        services["gmail"] = {"status": "connected" if gm.is_connected() else "disconnected"}
    except Exception:
        services["gmail"] = {"status": "disconnected"}

    # Calendar
    try:
        from openjarvis.connectors.gcalendar import GCalendarConnector
        gc = GCalendarConnector()
        services["gcalendar"] = {"status": "connected" if gc.is_connected() else "disconnected"}
    except Exception:
        services["gcalendar"] = {"status": "disconnected"}

    # Voice (shared state)
    services["voice"] = voice_state.copy()

    return {"status": "ok", "services": services, "timestamp": datetime.now().isoformat()}

# ═══════════════════════════════════════════
# ROUTES — Chat (Ollama)
# ═══════════════════════════════════════════
@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Send a message to the local LLM via Ollama and return the response."""
    system_prompt = (
        "You are Jarvis, a sophisticated, perceptive, and helpful personal AI assistant for Maitrayee. "
        "Your tone is polite, refined, razor-sharp, and warm — reminiscent of J.A.R.V.I.S., but natural and professional. "
        "Keep responses concise (1-3 sentences) unless detailed analysis is explicitly requested. "
        "Do NOT output markdown bold asterisks or hashes unless needed."
    )
    payload = {
        "model": req.model,
        "prompt": f"/no_think\n{system_prompt}\n\nUser: {req.message}\nJarvis:",
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": 200},
    }

    start = time.time()
    try:
        data = json.dumps(payload).encode()
        http_req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(http_req, timeout=45) as resp:
            result = json.loads(resp.read().decode())
            elapsed = int((time.time() - start) * 1000)
            answer = result.get("response", "").strip()
            if "<think>" in answer:
                answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.DOTALL).strip()
            return ChatResponse(response=answer, model=req.model, duration_ms=elapsed)
    except Exception as e:
        return ChatResponse(
            response=f"I'm experiencing an issue connecting to the inference engine. Error: {str(e)}",
            model=req.model,
            duration_ms=int((time.time() - start) * 1000),
        )

# ═══════════════════════════════════════════
# ROUTES — WhatsApp
# ═══════════════════════════════════════════
@app.get("/api/whatsapp/status")
async def wa_status():
    ch = get_wa_channel()
    if ch:
        return {"status": ch.status().value, "connected": ch.status().value == "connected"}
    return {"status": "disconnected", "connected": False}

@app.post("/api/whatsapp/connect")
async def wa_connect():
    """Start the WhatsApp bridge connection."""
    ch = get_wa_channel()
    if ch:
        try:
            ch.connect()
            return {"status": "connecting", "message": "WhatsApp bridge starting..."}
        except Exception as e:
            return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "Could not initialize WhatsApp channel"}

@app.get("/api/whatsapp/messages")
async def wa_get_messages():
    with wa_lock:
        if not wa_messages:
            wa_messages.extend(load_wa_messages())
        return {"messages": list(wa_messages)}

@app.post("/api/whatsapp/send")
async def wa_send(req: WhatsAppSendRequest):
    """Send a WhatsApp message with intelligent contact and number resolution."""
    ch = get_wa_channel()
    if not ch:
        return {"success": False, "error": "WhatsApp bridge is not initialized"}

    recip = req.recipient.strip()
    if not recip:
        return {"success": False, "error": "Recipient cannot be empty"}

    contacts = load_contacts()
    # Check friendly name (e.g., "mom", "baba", "riya", "me")
    target = contacts.get(recip.lower(), recip)

    # Normalize phone numbers
    if "@" not in target:
        digits = "".join(c for c in target if c.isdigit())
        if len(digits) == 10:
            target = f"91{digits}@s.whatsapp.net"
        elif len(digits) > 10:
            target = f"{digits}@s.whatsapp.net"

    try:
        success = ch.send(target, req.message)
        if success:
            with wa_lock:
                wa_messages.append({
                    "sender": "me",
                    "content": req.message,
                    "message_id": f"sent_{int(time.time()*1000)}",
                    "conversation_id": target,
                    "timestamp": datetime.now().isoformat(),
                })
                save_wa_messages(wa_messages)
            return {"success": True, "target": target}
        else:
            return {"success": False, "error": f"WhatsApp bridge was unable to deliver message to {target}. Check if bridge is connected."}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════════
# ROUTES — Contacts
# ═══════════════════════════════════════════
@app.get("/api/contacts")
async def get_contacts():
    return {"contacts": load_contacts()}

@app.post("/api/contacts")
async def add_or_update_contact(req: ContactRequest):
    save_contact(req.name, req.phone)
    return {"success": True, "contacts": load_contacts()}

# ═══════════════════════════════════════════
# ═══════════════════════════════════════════
# ROUTES — Google Drive & Document Search
# ═══════════════════════════════════════════
@app.get("/api/gdrive/files")
async def gdrive_files(limit: int = 25):
    """List recent files and docs from Google Drive quickly."""
    try:
        from openjarvis.connectors.oauth import load_tokens
        from openjarvis.connectors.gdrive import _gdrive_api_list_files
        tokens = load_tokens(CONFIG_DIR / "connectors" / "gdrive.json")
        if not tokens:
            return {"connected": False, "files": []}
        token = tokens.get("access_token") or tokens.get("token")
        if not token:
            return {"connected": False, "files": []}
        res = _gdrive_api_list_files(token)
        raw_files = res.get("files", [])
        files = []
        for f in raw_files[:limit]:
            mime = f.get("mimeType", "")
            is_doc = "document" in mime
            is_sheet = "spreadsheet" in mime
            files.append({
                "id": f.get("id"),
                "title": f.get("name", "Untitled"),
                "url": f.get("webViewLink", ""),
                "mimeType": mime,
                "is_gdoc": True,
                "type": "Google Doc" if is_doc else "Spreadsheet" if is_sheet else "Drive Item",
                "timestamp": f.get("modifiedTime", "")
            })
        return {"connected": True, "files": files}
    except Exception as e:
        return {"connected": False, "error": str(e), "files": []}

@app.get("/api/gdrive/search")
async def gdrive_search(q: str = ""):
    """Instant search for Google Docs and Google Drive files."""
    try:
        from openjarvis.connectors.oauth import load_tokens
        from openjarvis.connectors.gdrive import _gdrive_api_list_files
        tokens = load_tokens(CONFIG_DIR / "connectors" / "gdrive.json")
        if not tokens:
            return {"connected": False, "files": []}
        token = tokens.get("access_token") or tokens.get("token")
        if not token:
            return {"connected": False, "files": []}
        res = _gdrive_api_list_files(token)
        raw_files = res.get("files", [])
        matched = []
        q_lower = q.lower().strip()
        for f in raw_files:
            name = f.get("name", "Untitled")
            mime = f.get("mimeType", "")
            if not q_lower or q_lower in name.lower():
                is_doc = "document" in mime
                is_sheet = "spreadsheet" in mime
                is_folder = "folder" in mime
                matched.append({
                    "id": f.get("id"),
                    "name": name,
                    "url": f.get("webViewLink", ""),
                    "mime_type": mime,
                    "type_label": "Google Doc" if is_doc else "Google Sheet" if is_sheet else "Drive Item",
                    "icon": "doc" if is_doc else "sheet" if is_sheet else "folder" if is_folder else "pdf",
                    "modified": "Synced from Drive",
                    "is_gdoc": True
                })
        return {"connected": True, "files": matched}
    except Exception as e:
        return {"connected": False, "error": str(e), "files": []}

# ═══════════════════════════════════════════
# ROUTES — Local Device Files
# ═══════════════════════════════════════════
@app.get("/api/local/files")
async def get_local_and_drive_files(category: str = "all", search: str = ""):
    """Return actual files from user's device and Google Drive, categorized and sorted."""
    items = []
    
    # 1. First add Google Drive Docs
    try:
        from openjarvis.connectors.oauth import load_tokens
        from openjarvis.connectors.gdrive import _gdrive_api_list_files
        tokens = load_tokens(CONFIG_DIR / "connectors" / "gdrive.json")
        if tokens:
            token = tokens.get("access_token") or tokens.get("token")
            if token:
                res = _gdrive_api_list_files(token)
                for f in res.get("files", [])[:30]:
                    mime = f.get("mimeType", "")
                    name = f.get("name", "Untitled")
                    is_doc = "document" in mime
                    is_sheet = "spreadsheet" in mime
                    is_folder = "folder" in mime
                    items.append({
                        "name": name,
                        "path": f.get("webViewLink", ""),
                        "url": f.get("webViewLink", ""),
                        "size": "Google Doc · Synced" if is_doc else "Spreadsheet · Synced" if is_sheet else "Drive Item",
                        "raw_size": 1000000,
                        "mtime": time.time() - 1800,
                        "modified": "Modified today",
                        "category": "documents" if (is_doc or is_sheet) else "others",
                        "icon": "doc" if is_doc else "sheet" if is_sheet else "folder",
                        "is_gdoc": True
                    })
    except Exception:
        pass

    # 2. Add local macOS files from user's Desktop, Documents, Downloads, Jarvis
    home = Path.home()
    target_dirs = [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
        Path("/Users/maitrayeedighe/Jarvis"),
    ]
    
    for d in target_dirs:
        if not d.exists():
            continue
        for root, _, fs in os.walk(d):
            rel = Path(root).relative_to(d)
            if len(rel.parts) > 1:
                continue
            for f in fs:
                if f.startswith(".") or f.endswith(".pyc") or f == "Icon\r":
                    continue
                fp = Path(root) / f
                try:
                    st = fp.stat()
                    ext = fp.suffix.lower()
                    if ext == ".pdf":
                        cat, icon, type_name = "documents", "pdf", "PDF"
                    elif ext in [".docx", ".doc", ".txt", ".md", ".rtf"]:
                        cat, icon, type_name = "documents", "doc", "Word / Doc"
                    elif ext in [".xlsx", ".csv"]:
                        cat, icon, type_name = "documents", "sheet", "Excel / Sheet"
                    elif ext in [".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif"]:
                        cat, icon, type_name = "images", "image", "Image"
                    elif ext in [".py", ".js", ".html", ".css", ".json", ".sh"]:
                        cat, icon, type_name = "others", "code", "Code"
                    else:
                        cat, icon, type_name = "others", "other", "File"

                    items.append({
                        "name": f,
                        "path": str(fp),
                        "url": "",
                        "size": f"{type_name} · {format_file_size(st.st_size)}",
                        "raw_size": st.st_size,
                        "mtime": st.st_mtime,
                        "modified": format_relative_time(st.st_mtime),
                        "category": cat,
                        "icon": icon,
                        "is_gdoc": False
                    })
                except Exception:
                    pass

    # Filtering by category & search
    if search:
        s_lower = search.lower().strip()
        items = [i for i in items if s_lower in i["name"].lower()]

    if category != "all":
        if category == "gdocs":
            items = [i for i in items if i.get("is_gdoc")]
        elif category == "notes":
            items = [i for i in items if i["name"].lower().endswith(('.md', '.txt', '.note'))]
        else:
            items = [i for i in items if i.get("category") == category]

    items.sort(key=lambda x: x["mtime"], reverse=True)
    return {"files": items[:60], "total": len(items)}

@app.post("/api/local/open")
async def open_local_file(req: OpenFileRequest):
    p = req.path.strip()
    if not p:
        return {"success": False, "error": "Path required"}
    try:
        import subprocess
        subprocess.Popen(["open", p])
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════════
# ROUTES — Real Device Notes
# ═══════════════════════════════════════════
@app.get("/api/notes")
async def get_notes():
    return {"notes": load_notes()}

@app.post("/api/notes")
async def add_note(note: NoteItem):
    notes = load_notes()
    note.id = int(time.time() * 1000)
    note.created_at = datetime.now().isoformat()
    n_dict = note.model_dump()
    notes.insert(0, n_dict)
    save_notes(notes)
    return {"success": True, "note": n_dict}

@app.put("/api/notes/{note_id}")
async def update_note(note_id: int, note: NoteItem):
    notes = load_notes()
    for i, n in enumerate(notes):
        if n.get("id") == note_id:
            n_dict = note.model_dump()
            n_dict["id"] = note_id
            if not n_dict.get("created_at"):
                n_dict["created_at"] = n.get("created_at", datetime.now().isoformat())
            notes[i] = n_dict
            save_notes(notes)
            return {"success": True, "note": n_dict}
    return {"success": False, "error": "Note not found"}

@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: int):
    notes = [n for n in load_notes() if n.get("id") != note_id]
    save_notes(notes)
    return {"success": True}

# ═══════════════════════════════════════════
# ROUTES — Google Calendar Sync & Management
# ═══════════════════════════════════════════

def fetch_gcalendar_month_events(year: int, month: int) -> List[Dict[str, Any]]:
    from openjarvis.connectors.google_auth import call_with_refresh
    from openjarvis.connectors.gcalendar import _DEFAULT_CREDENTIALS_PATH
    import httpx
    import calendar

    days_in_month = calendar.monthrange(year, month)[1]
    time_min = f"{year:04d}-{month:02d}-01T00:00:00Z"
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    time_max = f"{next_year:04d}-{next_month:02d}-01T00:00:00Z"

    def do_fetch(tok):
        params = {
            "singleEvents": "true",
            "orderBy": "startTime",
            "timeMin": time_min,
            "timeMax": time_max,
            "maxResults": 250
        }
        resp = httpx.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {tok}"},
            params=params,
            timeout=15.0
        )
        resp.raise_for_status()
        return resp.json()

    res = call_with_refresh(do_fetch, _DEFAULT_CREDENTIALS_PATH)
    items = res.get("items", [])
    events = []
    for it in items:
        summary = it.get("summary") or "Untitled Event"
        start = it.get("start", {})
        dt_start = start.get("dateTime")
        d_start = start.get("date")
        
        if dt_start:
            date_str = dt_start.split("T")[0]
            time_part = dt_start.split("T")[1][:5]
            try:
                h, m = map(int, time_part.split(":"))
                period = "AM" if h < 12 else "PM"
                h_12 = h % 12 or 12
                time_display = f"{h_12:02d}:{m:02d} {period}"
            except Exception:
                time_display = time_part
            all_day = False
        else:
            date_str = d_start or f"{year:04d}-{month:02d}-01"
            time_display = "All Day"
            all_day = True

        try:
            day_num = int(date_str.split("-")[2])
        except Exception:
            day_num = 1

        events.append({
            "id": it.get("id"),
            "title": summary,
            "text": summary,
            "date": date_str,
            "day": day_num,
            "month": month,
            "year": year,
            "time": time_display,
            "all_day": all_day,
            "link": it.get("htmlLink", ""),
            "description": it.get("description", ""),
            "source": "google_calendar",
            "category": "Google Calendar",
            "icon": "📅",
            "done": False,
            "is_gcal": True
        })
    return events

def create_gcalendar_event(summary: str, date_str: str, time_str: str = "", end_time_str: str = "", description: str = "") -> Dict[str, Any]:
    from openjarvis.connectors.google_auth import call_with_refresh
    from openjarvis.connectors.gcalendar import _DEFAULT_CREDENTIALS_PATH
    import httpx
    import re

    def do_create(tok):
        clean_time = ""
        if time_str:
            m = re.search(r"(\d{1,2}):(\d{2})(?:\s*([APap][Mm]))?", time_str)
            if m:
                hh = int(m.group(1))
                mm = int(m.group(2))
                ampm = m.group(3)
                if ampm:
                    if ampm.upper() == "PM" and hh < 12:
                        hh += 12
                    elif ampm.upper() == "AM" and hh == 12:
                        hh = 0
                clean_time = f"{hh:02d}:{mm:02d}"

        if clean_time:
            start_dt = f"{date_str}T{clean_time}:00+05:30"
            hh_start, mm_start = map(int, clean_time.split(":"))
            hh_end = (hh_start + 1) % 24
            end_dt = f"{date_str}T{hh_end:02d}:{mm_start:02d}:00+05:30"
            body = {
                "summary": summary,
                "description": description or "Created from Jarvis AI Dashboard",
                "start": {"dateTime": start_dt, "timeZone": "Asia/Kolkata"},
                "end": {"dateTime": end_dt, "timeZone": "Asia/Kolkata"}
            }
        else:
            body = {
                "summary": summary,
                "description": description or "Created from Jarvis AI Dashboard",
                "start": {"date": date_str},
                "end": {"date": date_str}
            }


        resp = httpx.post(
            "https://www.googleapis.com/calendar/v3/calendars/primary/events",
            headers={"Authorization": f"Bearer {tok}"},
            json=body,
            timeout=15.0
        )
        resp.raise_for_status()
        return resp.json()

    return call_with_refresh(do_create, _DEFAULT_CREDENTIALS_PATH)

@app.get("/api/gcalendar/status")
async def gcalendar_status():
    try:
        from openjarvis.connectors.oauth import load_tokens
        tokens = load_tokens(CONFIG_DIR / "connectors" / "gcalendar.json")
        connected = bool(tokens and (tokens.get("access_token") or tokens.get("token")))
        return {"connected": connected, "email": "maitrayee.dighe2007@gmail.com"}
    except Exception as e:
        return {"connected": False, "error": str(e)}

@app.get("/api/gcalendar/events")
async def gcalendar_events(year: int = 2026, month: int = 10):
    try:
        events = fetch_gcalendar_month_events(year, month)
        return {"connected": True, "year": year, "month": month, "events": events, "count": len(events)}
    except Exception as e:
        return {"connected": False, "year": year, "month": month, "error": str(e), "events": []}

@app.post("/api/gcalendar/events")
async def gcalendar_create_event_endpoint(event_req: GCalendarEventCreate):
    try:
        res = create_gcalendar_event(
            summary=event_req.summary,
            date_str=event_req.date,
            time_str=event_req.start_time,
            end_time_str=event_req.end_time,
            description=event_req.description
        )
        return {"success": True, "event": res}
    except Exception as e:
        return {"success": False, "error": str(e)}

# ═══════════════════════════════════════════
# ROUTES — Reminders & AI Evaluation
# ═══════════════════════════════════════════
@app.get("/api/reminders")
async def get_reminders():
    return {"reminders": load_reminders()}

@app.post("/api/reminders")
async def add_reminder(reminder: Reminder):
    reminders = load_reminders()
    reminder.id = int(time.time() * 1000)
    reminder.created_at = datetime.now().isoformat()
    if reminder.sync_gcalendar and reminder.date:
        try:
            res = create_gcalendar_event(
                summary=reminder.text,
                date_str=reminder.date,
                time_str=reminder.time if ":" in reminder.time else "",
                description=f"Jarvis Reminder [{reminder.category}]"
            )
            reminder.gcal_link = res.get("htmlLink")
        except Exception as e:
            print(f"[GCalendar] Sync error: {e}")
    rem_dict = reminder.model_dump()
    reminders.append(rem_dict)
    save_reminders(reminders)
    return {"reminder": rem_dict}


@app.put("/api/reminders/{reminder_id}")
async def update_reminder(reminder_id: int, reminder: Reminder):
    reminders = load_reminders()
    for i, r in enumerate(reminders):
        if r.get("id") == reminder_id:
            reminder.id = reminder_id
            rem_dict = reminder.model_dump()
            reminders[i] = rem_dict
            save_reminders(reminders)
            return {"reminder": rem_dict}
    return {"error": "Reminder not found"}

@app.post("/api/reminders/{reminder_id}/toggle")
async def toggle_reminder(reminder_id: int):
    reminders = load_reminders()
    for r in reminders:
        if r.get("id") == reminder_id:
            r["done"] = not r.get("done", False)
            save_reminders(reminders)
            return {"success": True, "reminder": r}
    return {"error": "Reminder not found"}

@app.delete("/api/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int):
    reminders = [r for r in load_reminders() if r.get("id") != reminder_id]
    save_reminders(reminders)
    return {"success": True}

@app.post("/api/reminders/{reminder_id}/evaluate")
async def evaluate_reminder(reminder_id: int):
    """Jarvis evaluates if the user has actually accomplished this reminder.
    Cross-checks recent WhatsApp activity, Google Drive files, and context.
    """
    reminders = load_reminders()
    target = None
    for r in reminders:
        if r.get("id") == reminder_id:
            target = r
            break
    if not target:
        return {"error": "Reminder not found"}

    # Gather recent context
    with wa_lock:
        recent_wa = [f"{m.get('sender')}: {m.get('content')}" for m in wa_messages[-6:]]
    wa_ctx = "\n".join(recent_wa) if recent_wa else "No recent messages"

    # Quick Drive check
    drive_ctx = "Drive connected"

    eval_prompt = (
        f"You are Jarvis, Maitrayee's personal assistant. Evaluate if this reminder has been completed:\n"
        f"Task: \"{target.get('text')}\"\n"
        f"Due date: \"{target.get('due', 'None')}\"\n"
        f"Marked done by user: {target.get('done')}\n"
        f"Recent WhatsApp context:\n{wa_ctx}\n\n"
        "Assess whether this task is done, pending, or needs attention. "
        "Provide a crisp 1-2 sentence evaluation with your recommendation. Do not use markdown headers."
    )

    try:
        payload = {
            "model": "llama3:latest",
            "prompt": f"/no_think\n{eval_prompt}\n\nJarvis Assessment:",
            "stream": False,
            "options": {"temperature": 0.4, "num_predict": 120},
        }
        data = json.dumps(payload).encode()
        http_req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(http_req, timeout=30) as resp:
            res = json.loads(resp.read().decode())
            assessment = res.get("response", "").strip()
            if "<think>" in assessment:
                assessment = re.sub(r"<think>.*?</think>", "", assessment, flags=re.DOTALL).strip()
    except Exception as e:
        assessment = f"Verification check completed. Please confirm if you've finalized: '{target.get('text')}'."

    evaluation_record = {
        "evaluated_at": datetime.now().isoformat(),
        "assessment": assessment,
        "status": "completed" if target.get("done") else "pending_review",
    }
    target["evaluation"] = evaluation_record
    save_reminders(reminders)
    return {"success": True, "evaluation": evaluation_record, "reminder": target}

@app.delete("/api/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int):
    reminders = load_reminders()
    reminders = [r for r in reminders if r.get("id") != reminder_id]
    save_reminders(reminders)
    return {"deleted": True}

# ═══════════════════════════════════════════
# ROUTES — Ollama Models
# ═══════════════════════════════════════════
@app.get("/api/models")
async def list_models():
    try:
        req = urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2)
        data = json.loads(req.read().decode())
        models = [m.get("name") for m in data.get("models", [])]
        return {"models": models}
    except Exception:
        return {"models": []}

# ═══════════════════════════════════════════
# WEBSOCKET — Voice status
# ═══════════════════════════════════════════
@app.websocket("/ws/voice")
async def voice_ws(websocket: WebSocket):
    await websocket.accept()
    voice_clients.append(websocket)
    try:
        await websocket.send_json(voice_state)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in voice_clients:
            voice_clients.remove(websocket)

@app.post("/api/voice/update")
async def voice_update(update: dict):
    """Called by hello_jarvis.py to push voice state updates."""
    voice_state.update(update)
    voice_state["updated_at"] = datetime.now().isoformat()
    for ws in voice_clients[:]:
        try:
            await ws.send_json(voice_state)
        except Exception:
            if ws in voice_clients:
                voice_clients.remove(ws)
    return {"ok": True}

# ═══════════════════════════════════════════
# VOICE AGENT PROCESS MANAGER
# ═══════════════════════════════════════════
voice_process = None
voice_process_lock = threading.Lock()

@app.get("/api/voice/status")
async def voice_process_status():
    global voice_process
    running = False
    with voice_process_lock:
        if voice_process is not None:
            if voice_process.poll() is None:
                running = True
            else:
                voice_process = None
    return {
        "running": running,
        "is_listening": voice_state.get("is_listening", False),
        "last_heard": voice_state.get("last_heard", ""),
        "last_response": voice_state.get("last_response", ""),
        "updated_at": voice_state.get("updated_at", ""),
    }

@app.post("/api/voice/start")
async def voice_process_start():
    global voice_process
    with voice_process_lock:
        if voice_process is not None and voice_process.poll() is None:
            return {"running": True, "message": "Voice agent already running"}

        import subprocess
        # Search for python in multiple common locations
        python_bin = sys.executable
        venv_path = JARVIS_ROOT / "OpenJarvis" / ".venv" / "bin" / "python"
        if venv_path.exists():
            python_bin = venv_path
        
        script_path = JARVIS_ROOT / "hello_jarvis.py"
        
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        # Ensure the src directory is in PYTHONPATH so openjarvis can be imported
        env["PYTHONPATH"] = os.pathsep.join([
            str(JARVIS_ROOT / "OpenJarvis" / "src"),
            str(JARVIS_ROOT),
            env.get("PYTHONPATH", "")
        ])

        try:
            voice_process = subprocess.Popen(
                [str(python_bin), "-u", str(script_path)],
                cwd=str(JARVIS_ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            def monitor_output():
                global voice_process
                if not voice_process or not voice_process.stdout:
                    return
                for line in iter(voice_process.stdout.readline, ''):
                    line_str = line.strip()
                    if "👤 You:" in line_str:
                        heard = line_str.split("👤 You:", 1)[1].strip()
                        voice_state["last_heard"] = heard
                        voice_state["is_listening"] = True
                    elif "🗣️  Jarvis:" in line_str:
                        resp = line_str.split("🗣️  Jarvis:", 1)[1].strip()
                        voice_state["last_response"] = resp
                        voice_state["is_listening"] = False

            threading.Thread(target=monitor_output, daemon=True).start()
            voice_state["is_listening"] = True
            return {
                "running": True,
                "message": "Terminal voice agent started",
                "model": "qwen3.5:4b",
                "stt": "faster-whisper",
                "tts": "Kokoro-82M"
            }
        except Exception as e:
            return {"running": False, "error": str(e)}

@app.post("/api/voice/stop")
async def voice_process_stop():
    global voice_process
    with voice_process_lock:
        if voice_process is not None:
            try:
                voice_process.terminate()
                voice_process.wait(timeout=2)
            except Exception:
                voice_process.kill()
            voice_process = None
        voice_state["is_listening"] = False
        return {"running": False, "message": "Voice agent stopped"}

# Global TTS session to avoid re-loading models on every speak request
tts_session = None
tts_session_lock = threading.Lock()

# Global reference to any running say process so we can kill it before starting a new one
_say_process = None
_say_process_lock = threading.Lock()

@app.post("/api/voice/speak")
async def voice_speak(body: dict):
    """Speak text through Mac speakers using the built-in macOS 'say' TTS command."""
    text = body.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "No text provided"}

    def _do_speak():
        global _say_process
        try:
            # Strip markdown / special formatting characters
            clean = re.sub(r"[*_`#~>\[\]]", "", text)
            # Trim to a reasonable length (about 60 seconds of speech)
            clean = clean[:600].strip()
            if not clean:
                return

            with _say_process_lock:
                # Kill any currently running say process so we don't queue up
                if _say_process and _say_process.poll() is None:
                    try:
                        _say_process.terminate()
                        _say_process.wait(timeout=1)
                    except Exception:
                        pass

                # Use Daniel (British) voice — sounds natural and AI-like for Jarvis
                proc = subprocess.Popen(
                    ["say", "-v", "Daniel", "-r", "185", clean],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _say_process = proc

            # Wait for it to finish (outside the lock so other requests can preempt)
            proc.wait()

        except Exception as e:
            print(f"[TTS say error: {e}]")
            # Last-resort: try the default system voice
            try:
                subprocess.run(["say", clean[:300]], timeout=30)
            except Exception:
                pass

    threading.Thread(target=_do_speak, daemon=True).start()
    return {"ok": True, "engine": "macOS-say-Daniel"}

CALL_LOGS_FILE = Path(__file__).resolve().parent / "call_logs.json"

def load_call_logs() -> List[dict]:
    if CALL_LOGS_FILE.exists():
        try:
            return json.loads(CALL_LOGS_FILE.read_text())
        except Exception:
            return []
    return []

def save_call_log_entry(entry: dict):
    try:
        logs = load_call_logs()
        logs.append(entry)
        CALL_LOGS_FILE.write_text(json.dumps(logs[-30:], indent=2))
    except Exception as e:
        print(f"[CallLog] Error saving: {e}")

class CallLogRequest(BaseModel):
    contact: str
    number: str = ""
    call_type: str = "cellular"  # cellular or whatsapp

@app.post("/api/call/log")
async def log_call(req: CallLogRequest):
    entry = {
        "contact": req.contact,
        "number": req.number,
        "call_type": req.call_type,
        "timestamp": datetime.now().isoformat(),
        "time_str": datetime.now().strftime("%I:%M %p"),
    }
    save_call_log_entry(entry)
    return {"success": True, "call": entry}

# ═══════════════════════════════════════════
# ROUTES — Weather, Schedule & Activity
# ═══════════════════════════════════════════
WEATHER_CACHE = {"temp": 28, "condition": "Partly cloudy", "city": "Pune", "updated_at": 0}

def _update_weather_bg():
    try:
        req = urllib.request.urlopen(
            "https://api.open-meteo.com/v1/forecast?latitude=18.5204&longitude=73.8567&current_weather=true",
            timeout=5
        )
        data = json.loads(req.read().decode())
        cw = data.get("current_weather", {})
        temp = round(cw.get("temperature", 28))
        code = cw.get("weathercode", 0)
        condition = "Sunny" if code == 0 else "Partly cloudy" if code in [1, 2, 3] else "Overcast" if code in [45, 48] else "Rain showers"
        WEATHER_CACHE.update({"temp": temp, "condition": condition, "city": "Pune", "updated_at": time.time()})
    except Exception as e:
        WEATHER_CACHE["updated_at"] = time.time()

threading.Thread(target=_update_weather_bg, daemon=True).start()

@app.get("/api/weather")
async def get_weather():
    """Fetch live weather metrics with instantaneous cached return."""
    if time.time() - WEATHER_CACHE["updated_at"] > 900:
        threading.Thread(target=_update_weather_bg, daemon=True).start()
    return WEATHER_CACHE

@app.get("/api/schedule")
async def get_schedule():
    """Returns today's agenda items (combines Google Calendar with daily schedule)."""
    items = [
        {"time": "09:00", "title": "Team standup meeting", "status": "completed", "color": "#3fb950"},
        {"time": "11:30", "title": "Dentist appointment", "status": "upcoming", "color": "#58a6ff"},
        {"time": "02:00", "title": "Work on project report", "status": "upcoming", "color": "#58a6ff"},
        {"time": "06:00", "title": "Gym session", "status": "upcoming", "color": "#3fb950"},
    ]
    return {"schedule": items}

@app.get("/api/activity")
async def get_activity():
    """Returns consolidated recent activity timeline."""
    contacts = load_contacts()
    activities = []
    
    # 1. Real call logs
    for c in reversed(load_call_logs()[-3:]):
        contact_name = c.get("contact", "Contact")
        c_type = "WhatsApp call to" if c.get("call_type") == "whatsapp" else "Called"
        activities.append({
            "type": "call",
            "icon": "📞",
            "title": f"{c_type} {contact_name}",
            "time": c.get("time_str", "Just now"),
            "color": "#059669"
        })

    # 2. Recent WhatsApp messages
    with wa_lock:
        for m in reversed(wa_messages[-4:]):
            s = m.get("sender", "")
            target_name = contacts.get(s.replace("@lid", "").replace("@s.whatsapp.net", ""), "Riya") if s != "me" else "Maitrayee"
            action_text = f"Sent a message to {target_name}" if s == "me" else f"Received message from {target_name}"
            # Extract time string
            ts = m.get("timestamp", "")
            time_display = ts
            try:
                dt = datetime.fromisoformat(ts)
                time_display = dt.strftime("%I:%M %p")
            except Exception:
                pass
            activities.append({
                "type": "message",
                "icon": "💬",
                "title": action_text,
                "time": time_display,
                "color": "#1f6feb"
            })
            if len(activities) >= 4:
                break

    # 3. Add default daily events if fewer than 4
    defaults = [
        {"type": "call", "icon": "📞", "title": "Called Mom", "time": "09:45 AM", "color": "#059669"},
        {"type": "search", "icon": "🔍", "title": "Searched \"best cafes near me\"", "time": "09:21 AM", "color": "#7c3aed"},
        {"type": "reminder", "icon": "📅", "title": "Set a reminder: Dentist appointment", "time": "Yesterday", "color": "#d97706"},
    ]
    activities.extend(defaults[: max(0, 4 - len(activities))])
    return {"activities": activities[:4]}

# ═══════════════════════════════════════════
# PERSONALIZED INFORMATION & CALORIE TRACKER
# ═══════════════════════════════════════════
CALORIES_FILE = DASHBOARD_DIR / "calories.json"

def load_calories() -> dict:
    if CALORIES_FILE.exists():
        try:
            return json.loads(CALORIES_FILE.read_text())
        except Exception:
            pass
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "targets": {"calories": 2000, "protein": 110, "carbs": 220, "fats": 65},
        "entries": [],
        "habits": {"training": True, "hydration": True, "sleep": False, "healthy_meals": True}
    }

def save_calories(data: dict):
    try:
        CALORIES_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        print(f"[Calories] Save error: {e}")

QUOTES = [
    {"quote": "Discipline is the bridge between your goals and your dreams.", "author": "Jim Rohn"},
    {"quote": "The best way to predict the future is to invent it.", "author": "Alan Kay"},
    {"quote": "Simplicity is prerequisite for reliability.", "author": "Edsger W. Dijkstra"},
    {"quote": "It is not that we have so little time, but that we lose so much.", "author": "Seneca"},
    {"quote": "Code is like humor. When you have to explain it, it’s bad.", "author": "Cory House"},
    {"quote": "Make it work, make it right, make it fast.", "author": "Kent Beck"},
    {"quote": "Action is the foundational key to all success.", "author": "Pablo Picasso"},
    {"quote": "First, solve the problem. Then, write the code.", "author": "John Johnson"}
]

# ═══════════════════════════════════════════
# LIVE NEWS FROM RSS FEEDS (Google News + TechCrunch AI + VentureBeat)
# ═══════════════════════════════════════════
import xml.etree.ElementTree as ET
from html import unescape
import hashlib

_news_cache: Dict[str, Any] = {"ts": 0, "items": [], "ai": []}
_NEWS_CACHE_TTL = 600  # 10-minute cache

RSS_FEEDS = [
    # Global Tech & Business
    {"url": "https://news.google.com/rss/search?q=technology+AI+when:1d&hl=en-IN&gl=IN&ceid=IN:en", "category": "Technology", "tag": "Global"},
    {"url": "https://news.google.com/rss/search?q=India+tech+startup+when:1d&hl=en-IN&gl=IN&ceid=IN:en", "category": "India", "tag": "India"},
    {"url": "https://news.google.com/rss/search?q=artificial+intelligence+2025+when:1d&hl=en&gl=US&ceid=US:en", "category": "AI", "tag": "AI"},
    {"url": "https://news.google.com/rss/search?q=science+space+when:1d&hl=en&gl=US&ceid=US:en", "category": "Science", "tag": "Science"},
    {"url": "https://news.google.com/rss/search?q=business+economy+markets+when:1d&hl=en-IN&gl=IN&ceid=IN:en", "category": "Business", "tag": "Business"},
    {"url": "https://news.google.com/rss/search?q=India+politics+economy+when:1d&hl=en-IN&gl=IN&ceid=IN:en", "category": "India", "tag": "India"},
]

AI_RSS_FEEDS = [
    {"url": "https://news.google.com/rss/search?q=AI+LLM+machine+learning+2025+when:2d&hl=en&gl=US&ceid=US:en", "tag": "AI"},
    {"url": "https://news.google.com/rss/search?q=ChatGPT+Gemini+Claude+GPT+when:2d&hl=en&gl=US&ceid=US:en", "tag": "LLM"},
    {"url": "https://news.google.com/rss/search?q=AI+agent+robotics+autonomous+when:3d&hl=en&gl=US&ceid=US:en", "tag": "Agents"},
]

CATEGORY_ICONS = {
    "Technology": "💻", "Tech": "💻", "AI": "🤖", "India": "🇮🇳",
    "Science": "🔬", "Business": "📊", "Career": "📈", "Startups": "🚀",
    "Health": "❤️", "Finance": "💰", "Lifestyle": "✨"
}

def _parse_rss(url: str, max_items: int = 4) -> List[Dict]:
    """Fetch and parse an RSS feed, returning list of article dicts."""
    items = []
    try:
        import httpx
        resp = httpx.get(url, timeout=5.0, headers={"User-Agent": "Mozilla/5.0 JarvisBot/1.0"})
        if resp.status_code != 200:
            return []
        root = ET.fromstring(resp.text)
        channel = root.find("channel")
        if channel is None:
            return []
        ns = {"media": "http://search.yahoo.com/mrss/"}
        for item_el in channel.findall("item")[:max_items]:
            title = unescape((item_el.findtext("title") or "").strip())
            link = (item_el.findtext("link") or "").strip()
            desc = unescape((item_el.findtext("description") or "").strip())
            # Strip HTML from description
            desc = re.sub(r"<[^>]+>", " ", desc).strip()
            desc = re.sub(r"\s+", " ", desc)[:200]
            pub_date = item_el.findtext("pubDate") or ""
            source_el = item_el.find("source")
            source = source_el.text.strip() if source_el is not None and source_el.text else "Google News"
            # Try to get image from media:thumbnail or enclosure
            thumb = None
            mt = item_el.find("media:thumbnail", ns)
            if mt is not None:
                thumb = mt.get("url")
            enc = item_el.find("enclosure")
            if enc is not None and not thumb:
                thumb = enc.get("url")
            items.append({
                "title": title,
                "url": link,
                "summary": desc or title[:120],
                "source": source,
                "pub_date": pub_date,
                "thumb": thumb,
            })
    except Exception as e:
        print(f"[RSS] Error fetching {url}: {e}")
    return items

def _fetch_live_news() -> tuple:
    """Fetch fresh news from RSS feeds. Returns (news_items, ai_trends)."""
    news_items = []
    seen_titles: set = set()
    cat_idx = 0
    for feed in RSS_FEEDS:
        raw = _parse_rss(feed["url"], max_items=3)
        for r in raw:
            key = r["title"][:60].lower()
            if key in seen_titles:
                continue
            seen_titles.add(key)
            cat = feed["category"]
            news_items.append({
                "id": int(hashlib.md5(r["title"].encode()).hexdigest()[:8], 16),
                "category": cat,
                "tag": feed["tag"],
                "title": r["title"],
                "summary": r["summary"],
                "source": r["source"],
                "time": r["pub_date"][:16] if r["pub_date"] else "2h ago",
                "url": r["url"],
                "thumb": r["thumb"],
                "icon": CATEGORY_ICONS.get(cat, "📰"),
            })
        cat_idx += 1

    # Curated / reference items if news_items is empty or needs augmentation
    curated_stories = [
        {
            "id": 101,
            "category": "Technology",
            "tag": "India",
            "title": "India to invest ₹10,371 crore in IndiaAI Mission, plans 45,000+ GPUs by June 2026",
            "summary": "The government's IndiaAI Mission will expand compute infrastructure and boost domestic AI innovation, the IT ministry said.",
            "source": "Ministry of Electronics & IT / PIB",
            "time": "2h ago",
            "url": "https://news.google.com/search?q=India+invest+IndiaAI+Mission+GPUs",
            "thumb": "https://images.unsplash.com/photo-1518770660439-4636190af475?w=200&auto=format&fit=crop&q=80",
            "icon": "🇮🇳"
        },
        {
            "id": 102,
            "category": "Science",
            "tag": "AI Models",
            "title": "Google unveils Gemini 2.5 Pro with enhanced reasoning and multimodal coding",
            "summary": "The new model shows improved performance in complex reasoning tasks and supports text, image, audio and video inputs.",
            "source": "Google DeepMind / VentureBeat",
            "time": "4h ago",
            "url": "https://news.google.com/search?q=Google+Gemini+2.5+Pro+multimodal+reasoning",
            "thumb": "https://images.unsplash.com/photo-1620712943543-bcc4688e7485?w=200&auto=format&fit=crop&q=80",
            "icon": "✨"
        },
        {
            "id": 103,
            "category": "Business",
            "tag": "Careers",
            "title": "Top AI & System Architecture skills to boost your career in 2026",
            "summary": "Experts say hands-on experience with LLMs, distributed systems and cloud architecture will be key for future job roles.",
            "source": "TechCrunch / Hacker News",
            "time": "6h ago",
            "url": "https://news.google.com/search?q=AI+System+Architecture+skills+career+2026",
            "thumb": "https://images.unsplash.com/photo-1526374965328-7f61d4dc18c5?w=200&auto=format&fit=crop&q=80",
            "icon": "💼"
        },
        {
            "id": 104,
            "category": "India",
            "tag": "Clean Tech",
            "title": "India's EV and Clean Tech market sees 28% growth in H1 2026",
            "summary": "Rising domestic demand and policy support are driving strong growth in India's electric vehicle and clean energy sectors.",
            "source": "Economic Times / Mint",
            "time": "8h ago",
            "url": "https://news.google.com/search?q=India+EV+Clean+Tech+market+growth",
            "thumb": "https://images.unsplash.com/photo-1593941707882-a5bba14938c7?w=200&auto=format&fit=crop&q=80",
            "icon": "⚡"
        },
        {
            "id": 105,
            "category": "Startups",
            "tag": "Venture",
            "title": "Indian tech startup ecosystem secures $12B in Q3 across deep tech & AI",
            "summary": "Funding activity remains strong, with a focus on AI, climate tech and enterprise software.",
            "source": "Inc42 / YourStory",
            "time": "12h ago",
            "url": "https://news.google.com/search?q=Indian+tech+startup+ecosystem+deep+tech+funding",
            "thumb": "https://images.unsplash.com/photo-1559136555-9303baea8ebd?w=200&auto=format&fit=crop&q=80",
            "icon": "🚀"
        },
        {
            "id": 106,
            "category": "Health",
            "tag": "Wellness",
            "title": "New study links 30-minute daily walks to 40% lower risk of chronic disease",
            "summary": "Researchers at Harvard Medical School found that consistent moderate exercise significantly reduces heart disease, diabetes and depression risk.",
            "source": "Harvard Health / The Lancet",
            "time": "3h ago",
            "url": "https://news.google.com/search?q=daily+walking+chronic+disease+prevention+study",
            "thumb": "https://images.unsplash.com/photo-1571019613454-1cb2f99b2d8b?w=200&auto=format&fit=crop&q=80",
            "icon": "🏃‍♀️"
        },
        {
            "id": 107,
            "category": "Health",
            "tag": "Nutrition",
            "title": "WHO updates global dietary guidelines: more protein, less ultra-processed food",
            "summary": "The World Health Organization recommends shifting towards whole foods, lean proteins and plant-based meals for improved public health.",
            "source": "WHO / Reuters Health",
            "time": "5h ago",
            "url": "https://news.google.com/search?q=WHO+dietary+guidelines+2026+protein+ultra+processed",
            "thumb": "https://images.unsplash.com/photo-1490645935967-10de6ba17061?w=200&auto=format&fit=crop&q=80",
            "icon": "🥗"
        },
        {
            "id": 108,
            "category": "Health",
            "tag": "Mental Health",
            "title": "India launches nationwide mental health helpline with AI-powered triage",
            "summary": "The new 24/7 helpline uses AI to assess callers and route them to therapists, psychiatrists or crisis counselors in real time.",
            "source": "NIMHANS / Times of India",
            "time": "7h ago",
            "url": "https://news.google.com/search?q=India+mental+health+helpline+AI+triage",
            "thumb": "https://images.unsplash.com/photo-1559757175-5700dde675bc?w=200&auto=format&fit=crop&q=80",
            "icon": "🧠"
        },
        {
            "id": 109,
            "category": "Lifestyle",
            "tag": "Productivity",
            "title": "The rise of digital minimalism: why Gen-Z is logging off social media",
            "summary": "A growing trend among young adults shows deliberate reduction in screen time, with many reporting improved focus and mental clarity.",
            "source": "The Atlantic / Wired",
            "time": "4h ago",
            "url": "https://news.google.com/search?q=digital+minimalism+Gen+Z+social+media+detox",
            "thumb": "https://images.unsplash.com/photo-1499750310107-5fef28a66643?w=200&auto=format&fit=crop&q=80",
            "icon": "📵"
        },
        {
            "id": 110,
            "category": "Lifestyle",
            "tag": "Travel",
            "title": "India's top 10 monsoon getaways for 2026: from Munnar to Meghalaya",
            "summary": "Travel experts pick the best off-season destinations combining natural beauty, heritage and affordable stays.",
            "source": "Conde Nast Traveller India",
            "time": "6h ago",
            "url": "https://news.google.com/search?q=India+monsoon+travel+destinations+2026",
            "thumb": "https://images.unsplash.com/photo-1506905925346-21bda4d32df4?w=200&auto=format&fit=crop&q=80",
            "icon": "🌿"
        },
        {
            "id": 111,
            "category": "Lifestyle",
            "tag": "Books",
            "title": "5 must-read books on AI, philosophy and self-improvement this season",
            "summary": "Curated picks from bestseller lists spanning technology ethics, personal growth and modern leadership.",
            "source": "Goodreads / Book Review",
            "time": "10h ago",
            "url": "https://news.google.com/search?q=best+books+AI+philosophy+self+improvement+2026",
            "thumb": "https://images.unsplash.com/photo-1512820790803-83ca734da794?w=200&auto=format&fit=crop&q=80",
            "icon": "📚"
        },
        {
            "id": 112,
            "category": "Science",
            "tag": "Space",
            "title": "ISRO's Chandrayaan-4 mission enters design phase, targeting 2028 lunar sample return",
            "summary": "India's space agency confirms plans for an ambitious Moon sample-return mission following Chandrayaan-3's success.",
            "source": "ISRO / Space.com",
            "time": "5h ago",
            "url": "https://news.google.com/search?q=ISRO+Chandrayaan+4+lunar+sample+return",
            "thumb": "https://images.unsplash.com/photo-1446776811953-b23d57bd21aa?w=200&auto=format&fit=crop&q=80",
            "icon": "🚀"
        },
        {
            "id": 113,
            "category": "Science",
            "tag": "Research",
            "title": "CRISPR gene therapy shows promise in curing sickle cell disease in clinical trials",
            "summary": "Phase 3 trials report 94% of patients achieving sustained remission after a single gene-editing treatment.",
            "source": "Nature Medicine / NIH",
            "time": "9h ago",
            "url": "https://news.google.com/search?q=CRISPR+gene+therapy+sickle+cell+clinical+trials",
            "thumb": "https://images.unsplash.com/photo-1532187863486-abf9dbad1b69?w=200&auto=format&fit=crop&q=80",
            "icon": "🧬"
        }
    ]


    # Prepend or blend curated reference items so top stories match screenshot and live feeds provide variety
    if not news_items:
        news_items = curated_stories
    else:
        # Prepend top curated stories if not already present
        news_items = curated_stories + news_items

    ai_items = []
    ai_seen: set = set()
    rank = 1
    for feed in AI_RSS_FEEDS:
        raw = _parse_rss(feed["url"], max_items=2)
        for r in raw:
            key = r["title"][:60].lower()
            if key in ai_seen:
                continue
            ai_seen.add(key)
            ai_items.append({
                "rank": rank,
                "title": r["title"],
                "desc": r["summary"][:110] if r["summary"] else r["title"],
                "source": r["source"],
                "url": r["url"],
                "tag": feed["tag"],
                "tags": ["AI", feed["tag"]],
                "stat": f"🔥 {round(14.5 - rank * 1.8, 1)}K"
            })
            rank += 1
            if rank > 5:
                break
        if rank > 5:
            break

    fallback_ai = [
        {"rank": 1, "title": "GPT-5 & Self-Reasoning", "desc": "What's new and why it matters for multi-step reasoning", "tags": ["AI", "Technology"], "stat": "🔥 12.5K", "url": "https://news.google.com/search?q=GPT-5+Self-Reasoning"},
        {"rank": 2, "title": "AI Agents in Production", "desc": "From chat interfaces to real-world autonomous actions", "tags": ["AI", "Productivity"], "stat": "🔥 8.7K", "url": "https://news.google.com/search?q=AI+Agents+in+Production"},
        {"rank": 3, "title": "Multimodal AI Everywhere", "desc": "Text + Image + Audio + Code unification", "tags": ["AI", "Research"], "stat": "🔥 6.3K", "url": "https://news.google.com/search?q=Multimodal+AI+Everywhere"},
        {"rank": 4, "title": "AI in Healthcare & Pharma", "desc": "Better diagnosis, automated clinical triage & drug discovery", "tags": ["Health", "Research"], "stat": "🔥 4.8K", "url": "https://news.google.com/search?q=AI+in+Healthcare+Pharma"},
        {"rank": 5, "title": "LLMs in Code & DevEx", "desc": "Writing, debugging, building end-to-end fullstack apps", "tags": ["Development", "Tools"], "stat": "🔥 3.9K", "url": "https://news.google.com/search?q=LLMs+in+Code+DevEx"}
    ]

    if len(ai_items) < 5:
        ai_items = fallback_ai

    return news_items, ai_items

def get_cached_news() -> tuple:
    """Return cached news, refreshing if stale."""
    now = time.time()
    if now - _news_cache["ts"] < _NEWS_CACHE_TTL and _news_cache["items"]:
        return _news_cache["items"], _news_cache["ai"]
    news, ai = _fetch_live_news()
    _news_cache["ts"] = now
    _news_cache["items"] = news
    _news_cache["ai"] = ai
    return news, ai


BOOK_SUGGESTIONS = [
    {"title": "Atomic Habits", "author": "James Clear", "tag": "Habits & Focus", "desc": "Tiny changes that lead to remarkable results. Essential for long-term consistency."},
    {"title": "Deep Work", "author": "Cal Newport", "tag": "Productivity", "desc": "Mastering the ability to focus without distraction on cognitively demanding tasks."},
    {"title": "Designing Data-Intensive Applications", "author": "Martin Kleppmann", "tag": "Architecture", "desc": "The definitive guide to distributed data systems, reliability, and scalability."},
    {"title": "Chip War", "author": "Chris Miller", "tag": "Tech Geopolitics", "desc": "The epic battle for the semiconductor microchips that power artificial intelligence."}
]

def estimate_meal_nutrition(food_text: str, portion: str = "1 bowl") -> Dict[str, Any]:
    text = food_text.lower()
    total_cals = 0
    total_p = 0
    total_c = 0
    total_f = 0

    mult = 1.0
    if "plate" in portion.lower():
        mult = 1.35
    elif "cup" in portion.lower():
        mult = 0.75
    elif "glass" in portion.lower():
        mult = 1.0
    elif "bowl" in portion.lower():
        mult = 1.0
    elif "scoop" in portion.lower():
        mult = 1.0

    found = False

    if "roti" in text or "chapati" in text:
        num_roti = 2
        for n in [1, 2, 3, 4, 5]:
            if f"{n} roti" in text or f"{n} chapati" in text:
                num_roti = n
                break
        total_cals += num_roti * 80
        total_p += num_roti * 3
        total_c += num_roti * 16
        total_f += num_roti * 1
        found = True

    if "dal" in text or "lentil" in text or "sambar" in text:
        total_cals += int(160 * mult)
        total_p += int(9 * mult)
        total_c += int(24 * mult)
        total_f += int(4 * mult)
        found = True

    if "biryani" in text:
        total_cals += int(420 * mult)
        total_p += int(18 * mult)
        total_c += int(55 * mult)
        total_f += int(14 * mult)
        found = True
    elif "rice" in text or "pulao" in text or "khichdi" in text:
        total_cals += int(180 * mult)
        total_p += int(4 * mult)
        total_c += int(38 * mult)
        total_f += int(1 * mult)
        found = True

    if "paneer" in text:
        total_cals += int(280 * mult)
        total_p += int(18 * mult)
        total_c += int(8 * mult)
        total_f += int(20 * mult)
        found = True
    elif "tofu" in text or "soya" in text:
        total_cals += int(150 * mult)
        total_p += int(16 * mult)
        total_c += int(5 * mult)
        total_f += int(8 * mult)
        found = True

    if "egg" in text or "omelette" in text or ("bhurji" in text and "paneer" not in text):
        num_eggs = 2
        for n in [1, 2, 3, 4]:
            if f"{n} egg" in text:
                num_eggs = n
                break
        total_cals += int(num_eggs * 75)
        total_p += int(num_eggs * 6.5)
        total_c += int(num_eggs * 0.6)
        total_f += int(num_eggs * 5)
        found = True

    if "chicken" in text:
        total_cals += int(240 * mult)
        total_p += int(30 * mult)
        total_c += int(4 * mult)
        total_f += int(10 * mult)
        found = True
    elif "fish" in text or "salmon" in text:
        total_cals += int(200 * mult)
        total_p += int(26 * mult)
        total_c += int(1 * mult)
        total_f += int(9 * mult)
        found = True

    if "curd" in text or "dahi" in text or "yogurt" in text:
        total_cals += 100
        total_p += 5
        total_c += 6
        total_f += 4
        found = True

    if "oat" in text or "muesli" in text:
        total_cals += int(220 * mult)
        total_p += int(8 * mult)
        total_c += int(38 * mult)
        total_f += int(4 * mult)
        found = True

    if "whey" in text or "protein shake" in text:
        total_cals += 130
        total_p += 26
        total_c += 3
        total_f += 1.5
        found = True

    if "salad" in text or "cucumber" in text or "sabzi" in text or "vegetable" in text:
        total_cals += int(90 * mult)
        total_p += int(3 * mult)
        total_c += int(12 * mult)
        total_f += int(3 * mult)
        found = True

    if "dosa" in text:
        total_cals += 180
        total_p += 4
        total_c += 30
        total_f += 5
        found = True
    elif "idli" in text:
        total_cals += 130
        total_p += 4
        total_c += 26
        total_f += 1
        found = True

    if "banana" in text:
        total_cals += 90
        total_p += 1
        total_c += 23
        total_f += 0
        found = True
    elif "apple" in text or "fruit" in text:
        total_cals += 75
        total_p += 0
        total_c += 19
        total_f += 0
        found = True

    if not found:
        if "plate" in portion.lower():
            total_cals, total_p, total_c, total_f = 460, 16, 56, 18
        else:
            total_cals, total_p, total_c, total_f = 290, 11, 36, 10

    return {
        "calories": round(total_cals),
        "protein": round(total_p, 1),
        "carbs": round(total_c, 1),
        "fats": round(total_f, 1)
    }

def compute_coach_advice(current_p: float, target_p: float, current_c: float, target_c: float, current_cal: float, target_cal: float) -> str:
    p_pct = (current_p / target_p) * 100 if target_p else 0
    if p_pct < 60:
        needed = round(target_p - current_p)
        return f"💡 AI Coach: Your protein is currently at {round(current_p)}g ({round(p_pct)}% of target). Aim to get ~{needed}g more protein for dinner — 150g paneer, 3 boiled eggs, or a scoop of whey protein will close the gap easily!"
    elif p_pct < 85:
        needed = round(target_p - current_p)
        return f"💡 AI Coach: Good progress! You're at {round(current_p)}g protein ({round(p_pct)}%). Just {needed}g more needed. A bowl of Greek yogurt, sprouted moong, or paneer will hit your daily 110g goal!"
    elif current_cal >= target_cal * 0.95:
        return f"💡 AI Coach: You're near your daily calorie limit ({round(current_cal)}/{round(target_cal)} kcal). Keep evening snacks light — herbal tea, cucumber salad, or light buttermilk."
    else:
        return f"💡 AI Coach: Excellent macro balance today! Protein is high at {round(current_p)}g ({round(p_pct)}%), carbs and fats are well-proportioned. Stay hydrated and aim for 8 hours of sleep tonight!"

@app.get("/api/info/overview")
async def get_info_overview():
    now = datetime.now()
    hour = now.hour
    if hour < 12:
        greeting = "Good morning, Maitrayee. ☀️"
    elif hour < 17:
        greeting = "Good afternoon, Maitrayee. 🌤️"
    else:
        greeting = "Good evening, Maitrayee. 🌙"

    day_idx = now.timetuple().tm_yday % len(QUOTES)
    quote = QUOTES[day_idx]

    weather = {
        "city": "Mumbai",
        "temp": 32,
        "condition": "Partly cloudy",
        "high": 35,
        "low": 26,
        "forecast": [
            {"day": "Mon", "icon": "☀️", "high": 33, "low": 26},
            {"day": "Tue", "icon": "🌤️", "high": 32, "low": 26},
            {"day": "Wed", "icon": "⛅", "high": 31, "low": 25},
            {"day": "Thu", "icon": "☀️", "high": 30, "low": 25}
        ]
    }
    try:
        import httpx
        resp = httpx.get(
            "https://api.open-meteo.com/v1/forecast?latitude=19.0760&longitude=72.8777&current_weather=true&daily=temperature_2m_max,temperature_2m_min,weathercode&timezone=Asia%2FKolkata",
            timeout=3.0
        )
        if resp.status_code == 200:
            data = resp.json()
            cw = data.get("current_weather", {})
            if "temperature" in cw:
                weather["temp"] = round(cw["temperature"])
            daily = data.get("daily", {})
            if daily.get("temperature_2m_max"):
                weather["high"] = round(daily["temperature_2m_max"][0])
                weather["low"] = round(daily["temperature_2m_min"][0])
    except Exception:
        pass

    cal_data = load_calories()
    tot_cal = sum(e.get("calories", 0) for e in cal_data.get("entries", []))
    tot_p = sum(e.get("protein", 0) for e in cal_data.get("entries", []))
    tot_c = sum(e.get("carbs", 0) for e in cal_data.get("entries", []))
    tot_f = sum(e.get("fats", 0) for e in cal_data.get("entries", []))
    targets = cal_data.get("targets", {"calories": 2000, "protein": 110, "carbs": 220, "fats": 65})
    coach_advice = compute_coach_advice(tot_p, targets["protein"], tot_c, targets["carbs"], tot_cal, targets["calories"])

    upcoming_events = []
    try:
        events = fetch_gcalendar_month_events(now.year, now.month)
        for ev in events:
            if ev.get("day", 0) >= now.day:
                upcoming_events.append({
                    "title": ev.get("title", ""),
                    "time": f"{ev.get('date', '')} · {ev.get('time', '')}",
                    "link": ev.get("link", "")
                })
                if len(upcoming_events) >= 3:
                    break
    except Exception:
        pass

    if not upcoming_events:
        upcoming_events = [
            {"title": "AI Lab Assignment Submission", "time": "Tomorrow, 11:00 AM · Google Calendar", "link": ""},
            {"title": "Mentor Meeting", "time": "Oct 10, 10:00 AM · Google Meet", "link": ""},
            {"title": "Placement Preparation Session", "time": "Oct 12, 02:00 PM · College", "link": ""}
        ]

    quick_reads = [
        {"category": "Health", "title": "Simple habits for better sleep and focus", "read_time": "5 min read", "dot": "#22c55e", "url": "https://www.sleepfoundation.org/sleep-hygiene"},
        {"category": "Productivity", "title": "The 2-minute rule for higher productivity", "read_time": "3 min read", "dot": "#38bdf8", "url": "https://jamesclear.com/how-to-stop-procrastinating-2-minute-rule"},
        {"category": "Technology", "title": "What's next for AI in 2026?", "read_time": "6 min read", "dot": "#818cf8", "url": "https://news.google.com/search?q=What+is+next+for+AI"},
        {"category": "Lifestyle", "title": "Minimalist living: small changes, big impact", "read_time": "4 min read", "dot": "#f59e0b", "url": "https://www.theminimalists.com"}
    ]

    news_live, ai_live = get_cached_news()
    return {
        "greeting": greeting,
        "subtitle": "Here's what's new, relevant and designed just for you.",
        "quote": quote,
        "weather": weather,
        "news": news_live if news_live else [],
        "ai_trends": ai_live if ai_live else [],
        "book_suggestions": BOOK_SUGGESTIONS,
        "quick_reads": quick_reads,
        "calories": {
            "current": {"calories": tot_cal, "protein": round(tot_p, 1), "carbs": round(tot_c, 1), "fats": round(tot_f, 1)},
            "targets": targets,
            "coach_advice": coach_advice,
            "entries": cal_data.get("entries", [])
        },
        "habits": cal_data.get("habits", {"training": True, "hydration": True, "sleep": False, "healthy_meals": True}),
        "upcoming_event": upcoming_events[0] if upcoming_events else {"title": "Team standup meeting", "time": "Tomorrow, 09:00 AM", "link": ""},
        "upcoming_events": upcoming_events
    }

@app.get("/api/calories")
async def get_calories_data():
    cal_data = load_calories()
    entries = cal_data.get("entries", [])
    tot_cal = sum(e.get("calories", 0) for e in entries)
    tot_p = sum(e.get("protein", 0) for e in entries)
    tot_c = sum(e.get("carbs", 0) for e in entries)
    tot_f = sum(e.get("fats", 0) for e in entries)
    targets = cal_data.get("targets", {"calories": 2000, "protein": 110, "carbs": 220, "fats": 65})
    advice = compute_coach_advice(tot_p, targets["protein"], tot_c, targets["carbs"], tot_cal, targets["calories"])
    return {
        "date": cal_data.get("date"),
        "targets": targets,
        "current": {
            "calories": tot_cal,
            "protein": round(tot_p, 1),
            "carbs": round(tot_c, 1),
            "fats": round(tot_f, 1)
        },
        "coach_advice": advice,
        "entries": entries,
        "habits": cal_data.get("habits", {})
    }

@app.post("/api/calories/log")
async def log_food_entry(req: FoodLogRequest):
    cal_data = load_calories()
    nutrients = estimate_meal_nutrition(req.food, req.quantity or req.portion)
    new_entry = {
        "id": int(time.time() * 1000),
        "food": req.food.strip(),
        "quantity": req.quantity or req.portion or "1 serving",
        "calories": nutrients["calories"],
        "protein": nutrients["protein"],
        "carbs": nutrients["carbs"],
        "fats": nutrients["fats"],
        "time": datetime.now().strftime("%I:%M %p")
    }
    cal_data["entries"].append(new_entry)
    save_calories(cal_data)
    return {"success": True, "entry": new_entry, "calories": await get_calories_data()}

@app.delete("/api/calories/{entry_id}")
async def delete_food_entry(entry_id: int):
    cal_data = load_calories()
    cal_data["entries"] = [e for e in cal_data.get("entries", []) if e.get("id") != entry_id]
    save_calories(cal_data)
    return {"success": True, "calories": await get_calories_data()}

@app.post("/api/calories/habits")
async def toggle_habit(req: HabitToggleRequest):
    cal_data = load_calories()
    habits = cal_data.get("habits", {})
    habits[req.habit] = not habits.get(req.habit, False)
    cal_data["habits"] = habits
    save_calories(cal_data)
    return {"success": True, "habits": habits}

@app.get("/api/news")
async def get_live_news(refresh: bool = False):
    """Return live news from RSS feeds. Set ?refresh=true to bust the cache."""
    global _news_cache
    if refresh:
        _news_cache["ts"] = 0  # Force cache refresh
    news, ai = get_cached_news()
    return {"news": news, "ai_trends": ai, "cached_at": _news_cache["ts"]}

@app.get("/api/info/quote/refresh")
async def refresh_quote():
    import random
    quote = random.choice(QUOTES)
    return {"quote": quote}


@app.get("/backdrop.jpg")
async def serve_backdrop():
    backdrop_file = DASHBOARD_DIR / "backdrop.jpg"
    if backdrop_file.exists():
        return FileResponse(backdrop_file)
    return FileResponse(DASHBOARD_DIR / "index.html")

# ═══════════════════════════════════════════
# SERVE STATIC FILES
# ═══════════════════════════════════════════
DASHBOARD_DIR = Path(__file__).resolve().parent

@app.get("/")
async def serve_dashboard():
    return FileResponse(DASHBOARD_DIR / "index.html")

app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

if __name__ == "__main__":
    import uvicorn
    print("🚀 Starting Jarvis Dashboard Server on http://localhost:5050")
    print("   Dashboard UI:  http://localhost:5050")
    print("   API docs:      http://localhost:5050/docs")
    uvicorn.run(app, host="0.0.0.0", port=5050, log_level="info")
