import sqlite3
import sys
import os
import datetime
import mimetypes
import json
import base64
import hashlib
import math
from pathlib import Path
import tkinter as tk
from tkinter import messagebox
from tkinter import StringVar
from tkinter import END
from tkinter import N, S, E, W
from tkinter import TclError
from tkinter import filedialog
from tkinter import ttk

# customtkinter UI
try:
    import customtkinter as ctk
except ImportError:
    ctk = None
import threading
import requests
from urllib.parse import urljoin, quote
import uuid

DB_FILENAME = "cloud_files.sqlite3"
CONFIG_FILENAME = "cloud_config.json"

# Force local-only vault mode by default. You can re-enable cloud in future if needed.
LOCAL_ONLY = True


def get_downloads_dir() -> str:
    """Return the OS-specific Downloads directory, creating it if needed."""
    # Prefer Windows USERPROFILE\Downloads if available
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        downloads = os.path.join(user_profile, "Downloads")
        try:
            os.makedirs(downloads, exist_ok=True)
            return downloads
        except Exception:
            pass
    # Fallback: ~/Downloads
    downloads = os.path.join(Path.home(), "Downloads")
    os.makedirs(downloads, exist_ok=True)
    return downloads


class CloudStorage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.config = self._load_config()
        self._ensure_database()

    def _load_config(self) -> dict:
        config_path = os.path.join(os.path.dirname(self.db_path), CONFIG_FILENAME)
        default_config = {
            "cloud_server_url": "https://your-cloud-server.com/api",
            "api_key": "your-api-key-here",
            "upload_endpoint": "/upload",
            "download_endpoint": "/download",
            "list_endpoint": "/list",
            "delete_endpoint": "/delete",
            "timeout": 30
        }
        
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                return {**default_config, **config}
            except Exception as e:
                print(f"Error loading config: {e}")
        
        # Create default config file
        with open(config_path, 'w') as f:
            json.dump(default_config, f, indent=2)
        
        return default_config

    def _ensure_database(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    local_name TEXT NOT NULL,
                    cloud_id TEXT,
                    cloud_name TEXT,
                    file_size INTEGER,
                    file_mime TEXT,
                    file_hash TEXT,
                    upload_date TEXT,
                    last_sync TEXT,
                    local_path TEXT,
                    is_synced INTEGER DEFAULT 0
                );
                """
            )
            
            # Add missing columns if they don't exist (for existing databases)
            try:
                conn.execute("ALTER TABLE files ADD COLUMN file_hash TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            
            try:
                conn.execute("ALTER TABLE files ADD COLUMN local_path TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add BLOB column to store file bytes when cloud is not configured
            try:
                conn.execute("ALTER TABLE files ADD COLUMN file_data BLOB")
            except sqlite3.OperationalError:
                pass  # Column already exists

    def _get_file_hash(self, file_path: str) -> str:
        """Generate MD5 hash of file for integrity checking"""
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _make_request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """Make HTTP request to cloud server"""
        url = urljoin(self.config["cloud_server_url"], endpoint)
        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "User-Agent": "CloudFileManager/1.0"
        }
        
        if "headers" in kwargs:
            headers.update(kwargs["headers"])
        
        kwargs["headers"] = headers
        kwargs["timeout"] = self.config["timeout"]
        
        response = requests.request(method, url, **kwargs)
        response.raise_for_status()
        return response

    def upload_file(self, file_path: str, cloud_name: str = None) -> dict:
        """Store bytes locally in SQLite (and upload to cloud only if enabled)"""
        file_path = Path(file_path)
        if not file_path.exists():
            raise Exception("File does not exist")
        
        if not cloud_name:
            cloud_name = file_path.name
        
        file_hash = self._get_file_hash(str(file_path))
        
        # Check if file already exists locally
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute(
                "SELECT id FROM files WHERE file_hash = ?", (file_hash,)
            ).fetchone()
            if existing:
                raise Exception("File already exists with same content")
        
        # If local-only, or placeholder server, just store locally
        if LOCAL_ONLY or "your-cloud-server.com" in self.config["cloud_server_url"]:
            cloud_id = str(uuid.uuid4())
            is_synced = 0  # Local only
        else:
            # Try to upload to cloud server
            try:
                with open(file_path, 'rb') as f:
                    files = {'file': (cloud_name, f, mimetypes.guess_type(str(file_path))[0] or 'application/octet-stream')}
                    data = {
                        'name': cloud_name,
                        'hash': file_hash,
                        'size': file_path.stat().st_size
                    }
                    
                    response = self._make_request('POST', self.config["upload_endpoint"], files=files, data=data)
                    result = response.json()
                    cloud_id = result.get('id', str(uuid.uuid4()))
                    is_synced = 1
            except Exception as e:
                print(f"Cloud upload failed, storing locally: {e}")
                cloud_id = str(uuid.uuid4())
                is_synced = 0
        
        # Store in local database
        with sqlite3.connect(self.db_path) as conn:
            file_bytes = None
            # Always store file bytes in SQLite vault
            file_bytes = file_path.read_bytes()
            conn.execute(
                """
                INSERT INTO files (local_name, cloud_id, cloud_name, file_size, file_mime, file_hash, upload_date, last_sync, local_path, is_synced, file_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    file_path.name,
                    cloud_id,
                    cloud_name,
                    file_path.stat().st_size,
                    mimetypes.guess_type(str(file_path))[0] or 'application/octet-stream',
                    file_hash,
                    datetime.datetime.utcnow().isoformat(),
                    datetime.datetime.utcnow().isoformat(),
                    str(file_path),
                    is_synced,
                    file_bytes
                )
            )
        
        return {"id": cloud_id, "name": cloud_name}

    def download_file(self, cloud_id: str, local_path: str) -> bool:
        """Download strictly from SQLite BLOB (local vault)."""
        # Local-only vault: read bytes from DB, fallback to original path
        if LOCAL_ONLY or "your-cloud-server.com" in self.config["cloud_server_url"]:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT local_path, file_data FROM files WHERE cloud_id = ?",
                    (cloud_id,)
                ).fetchone()
                if row is None:
                    raise Exception("File record not found")
                local_src, blob = row
                # If we already have the BLOB, write it out
                if blob is not None:
                    os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
                    with open(local_path, 'wb') as f:
                        f.write(blob)
                    return True
                # Backfill from original path if available, and persist into DB for next time
                if local_src and os.path.exists(local_src):
                    data = Path(local_src).read_bytes()
                    os.makedirs(os.path.dirname(local_path) or '.', exist_ok=True)
                    with open(local_path, 'wb') as f:
                        f.write(data)
                    conn.execute(
                        "UPDATE files SET file_data = ?, last_sync = ? WHERE cloud_id = ?",
                        (data, datetime.datetime.utcnow().isoformat(), cloud_id),
                    )
                    return True
                raise Exception("This entry has no stored file data and the original path no longer exists. Re-upload the file to store it in the vault.")
        
        try:
            response = self._make_request('GET', f"{self.config['download_endpoint']}/{cloud_id}")
            
            with open(local_path, 'wb') as f:
                f.write(response.content)
            
            return True
        except requests.exceptions.RequestException as e:
            raise Exception(f"Download failed: {e}")

    def delete_cloud_file(self, cloud_id: str) -> bool:
        """Delete file from cloud server"""
        # If using placeholder server, just return success
        if "your-cloud-server.com" in self.config["cloud_server_url"]:
            return True
        
        try:
            self._make_request('DELETE', f"{self.config['delete_endpoint']}/{cloud_id}")
            return True
        except requests.exceptions.RequestException as e:
            raise Exception(f"Delete failed: {e}")

    def get_cloud_files(self) -> list:
        """Get list of files from cloud server"""
        # Skip cloud if using placeholder URL
        if "your-cloud-server.com" in self.config["cloud_server_url"]:
            return []
        
        try:
            response = self._make_request('GET', self.config["list_endpoint"])
            return response.json().get('files', [])
        except requests.exceptions.RequestException as e:
            print(f"Failed to list cloud files: {e}")
            return []

    def get_local_files(self) -> list:
        """Get list of files from local database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM files ORDER BY upload_date DESC"
            ).fetchall()
        return rows

    def remove_local_file(self, file_id: int) -> None:
        """Remove file from local database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def test_connection(self) -> bool:
        """Test connection to cloud server"""
        try:
            response = self._make_request('GET', self.config["list_endpoint"])
            return response.status_code == 200
        except:
            return False


class CloudFileApp:
    def __init__(self, root: tk.Tk, storage: CloudStorage) -> None:
        self.root = root
        self.storage = storage
        
        self.root.title("Cloud Vault")
        self.root.geometry("1000x700")
        self._configure_style()
        
        # State variables

        self.search_var = StringVar(value="")
        self.status_var = StringVar(value="")
        
        # Build UI
        self._build_layout()
        self._bind_events()
        
        self.refresh_files()

    def _configure_style(self) -> None:
        if ctk is not None:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("blue")
            
            # Custom color scheme
            ctk.set_default_color_theme("blue")
            
            # Configure custom colors
            self.colors = {
                "primary": "#1f6aa5",
                "secondary": "#2b2b2b", 
                "accent": "#0078d4",
                "success": "#107c10",
                "warning": "#ff8c00",
                "error": "#d13438",
                "text": "#ffffff",
                "text_secondary": "#cccccc",
                "background": "#1e1e1e",
                "surface": "#2d2d30"
            }
            # Global fonts (Times New Roman)
            self.font_base = ctk.CTkFont(family="Times New Roman", size=12)
            self.font_small = ctk.CTkFont(family="Times New Roman", size=11)
            self.font_title = ctk.CTkFont(family="Times New Roman", size=24, weight="bold")
            self.font_subtitle = ctk.CTkFont(family="Times New Roman", size=14, weight="bold")
        else:
            self.colors = {}

    def _darken_color(self, color: str, factor: float) -> str:
        """Darken a hex color by a factor"""
        if not color.startswith('#'):
            return color
        try:
            r = int(color[1:3], 16)
            g = int(color[3:5], 16)
            b = int(color[5:7], 16)
            r = int(r * factor)
            g = int(g * factor)
            b = int(b * factor)
            return f"#{r:02x}{g:02x}{b:02x}"
        except:
            return color

    def _create_animated_command(self, command):
        """Create a command with button press animation"""
        def animated_command():
            # Simple animation effect - could be enhanced with threading for smooth transitions
            self.root.after(100, command)  # Small delay for visual feedback
        return animated_command

    # --- View switching (Upload / Library) ---
    def show_upload_view(self) -> None:
        if not ctk:
            return
        try:
            if hasattr(self, 'list_frame'):
                self.list_frame.pack_forget()
            if hasattr(self, 'instructions'):
                self.instructions.pack_forget()
            if hasattr(self, 'hero_frame'):
                self.hero_frame.pack(fill="x", pady=(0, 20))
        except Exception:
            pass

    def show_library_view(self) -> None:
        if not ctk:
            return
        try:
            if hasattr(self, 'hero_frame'):
                self.hero_frame.pack_forget()
            if hasattr(self, 'list_frame'):
                self.list_frame.pack(fill="both", expand=True, pady=(0, 20))
            if hasattr(self, 'instructions'):
                self.instructions.pack(fill="x", pady=(0, 20), padx=20)
        except Exception:
            pass

    def _build_layout(self) -> None:
        # Main container with modern styling
        if ctk:
            container = ctk.CTkFrame(self.root, fg_color="transparent")
            container.pack(fill="both", expand=True, padx=20, pady=20)
        else:
            container = ttk.Frame(self.root, padding=10)
            container.grid(row=0, column=0, sticky=(N, S, E, W))
            self.root.columnconfigure(0, weight=1)
            self.root.rowconfigure(0, weight=1)
            container.columnconfigure(0, weight=1)
        
        # Header with title, theme toggle and status
        header_frame = ctk.CTkFrame(container, fg_color=self.colors.get("surface", "#2d2d30"), corner_radius=15) if ctk else ttk.Frame(container)
        if ctk:
            header_frame.pack(fill="x", pady=(0, 20))
        else:
            header_frame.grid(row=0, column=0, sticky=(E, W), pady=(0, 10))
            header_frame.columnconfigure(0, weight=1)
        
        # Title
        if ctk:
            title_label = ctk.CTkLabel(header_frame, text="📁 Cloud Vault", font=self.font_title, text_color=self.colors.get("text", "#ffffff"))
            title_label.pack(pady=(20, 10))
            # Theme toggle
            toggle_frame = ctk.CTkFrame(header_frame, fg_color="transparent")
            toggle_frame.pack(fill="x", padx=20)
            ctk.CTkLabel(toggle_frame, text="Theme:", font=self.font_small).pack(side="left")
            def on_theme_change(choice: str):
                mode = "Light" if choice == "Light" else "Dark"
                ctk.set_appearance_mode(mode.lower())
            theme_toggle = ctk.CTkSegmentedButton(toggle_frame, values=["Light", "Dark"], command=on_theme_change)
            theme_toggle.set("Dark")
            theme_toggle.pack(side="left", padx=10)
        
        # Status bar with modern styling
        status_frame = ctk.CTkFrame(header_frame, fg_color="transparent") if ctk else ttk.Frame(header_frame)
        if ctk:
            status_frame.pack(fill="x", padx=20, pady=(0, 20))
        else:
            status_frame.grid(row=1, column=0, sticky=(E, W), pady=(0, 10))
            status_frame.columnconfigure(0, weight=1)
        
        server_url = self.storage.config["cloud_server_url"]
        if LOCAL_ONLY or "your-cloud-server.com" in server_url:
            status_text = "🔒 Local Vault Mode (SQLite)"
            status_color = self.colors.get("success", "#107c10")
        else:
            status_text = f"☁️ Cloud Server: {server_url}"
            status_color = self.colors.get("accent", "#0078d4")
        
        if ctk:
            status_label = ctk.CTkLabel(status_frame, text=status_text, font=self.font_subtitle, text_color=status_color)
            status_label.pack(side="left")
            status_value = ctk.CTkLabel(status_frame, textvariable=self.status_var, font=self.font_small, text_color=self.colors.get("text_secondary", "#cccccc"))
            status_value.pack(side="right")
        else:
            ttk.Label(status_frame, text=status_text).grid(row=0, column=0, sticky=W)
            ttk.Label(status_frame, textvariable=self.status_var).grid(row=0, column=1, sticky=E)
        
        # Top navbar (Upload / Library)
        if ctk:
            navbar = ctk.CTkFrame(container, fg_color="transparent")
            navbar.pack(fill="x", pady=(0, 12))
            def on_nav_change(choice: str):
                if choice == "Upload":
                    self.show_upload_view()
                else:
                    self.show_library_view()
            self.nav_tabs = ctk.CTkSegmentedButton(navbar, values=["Upload", "Library"], command=on_nav_change)
            self.nav_tabs.set("Upload")
            self.nav_tabs.pack(padx=20)

        # Central hero upload area (blue icon and big button)
        if ctk:
            self.hero_frame = ctk.CTkFrame(container, fg_color=self.colors.get("surface", "#2d2d30"), corner_radius=20)
            self.hero_frame.pack(fill="x", pady=(0, 20))
            icon = ctk.CTkLabel(self.hero_frame, text="☁️⬆️", text_color=self.colors.get("accent", "#0078d4"), font=ctk.CTkFont(family="Times New Roman", size=48, weight="bold"))
            icon.pack(pady=(20, 10))
            ctk.CTkLabel(self.hero_frame, text="Upload a file to your vault", font=self.font_subtitle).pack()
            ctk.CTkButton(self.hero_frame, text="Choose File to Upload", command=self._create_animated_command(self.upload_file),
                          fg_color=self.colors.get("primary", "#1f6aa5"), hover_color=self._darken_color(self.colors.get("primary", "#1f6aa5"), 0.85),
                          font=self.font_base, height=42, corner_radius=12).pack(pady=(12, 20))

        # Search and controls with modern styling
        controls_frame = ctk.CTkFrame(container, fg_color=self.colors.get("surface", "#2d2d30"), corner_radius=15) if ctk else ttk.Frame(container)
        if ctk:
            controls_frame.pack(fill="x", pady=(0, 20))
        else:
            controls_frame.grid(row=1, column=0, sticky=(E, W), pady=(0, 10))
            controls_frame.columnconfigure(1, weight=1)
        
        # Search section
        search_frame = ctk.CTkFrame(controls_frame, fg_color="transparent") if ctk else ttk.Frame(controls_frame)
        if ctk:
            search_frame.pack(fill="x", padx=20, pady=20)
        else:
            search_frame.grid(row=0, column=0, sticky=(E, W), pady=10)
            search_frame.columnconfigure(1, weight=1)
        
        if ctk:
            search_label = ctk.CTkLabel(search_frame, text="🔍 Search Files:", font=self.font_subtitle, text_color=self.colors.get("text", "#ffffff"))
            search_label.pack(side="left", padx=(0, 10))
            self.search_entry = ctk.CTkEntry(search_frame, textvariable=self.search_var, placeholder_text="Type to search files...", 
                                           font=self.font_base, width=300, height=35, corner_radius=10)
            self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 10))
        else:
            ttk.Label(search_frame, text="Search:").grid(row=0, column=0, padx=(0, 8))
            self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
            self.search_entry.grid(row=0, column=1, sticky=(E, W), padx=(0, 8))
        
        # Action buttons with modern styling
        button_frame = ctk.CTkFrame(controls_frame, fg_color="transparent") if ctk else ttk.Frame(controls_frame)
        if ctk:
            button_frame.pack(fill="x", padx=20, pady=(0, 20))
        else:
            button_frame.grid(row=1, column=0, sticky=(E, W), pady=10)
        
        # Button configurations
        button_configs = [
            ("📤 Upload File", self.upload_file, self.colors.get("primary", "#1f6aa5")),
            ("📥 Download", self.download_file, self.colors.get("success", "#107c10")),
            ("🗑️ Delete", self.delete_file, self.colors.get("error", "#d13438")),
            ("🔄 Refresh", self.refresh_files, self.colors.get("accent", "#0078d4"))
        ]
        
        if not LOCAL_ONLY:
            button_configs.extend([
                ("☁️ Sync All", self.sync_all, self.colors.get("warning", "#ff8c00")),
                ("⚙️ Settings", self.open_settings, self.colors.get("text_secondary", "#cccccc"))
            ])
        
        for i, (text, command, color) in enumerate(button_configs):
            if ctk:
                btn = ctk.CTkButton(button_frame, text=text, command=self._create_animated_command(command), 
                                  fg_color=color, hover_color=self._darken_color(color, 0.8),
                                  font=self.font_base, height=40, corner_radius=10)
                btn.pack(side="left", padx=5, pady=5)
                # Hover grow effect
                def on_enter_factory(b):
                    def _on_enter(_e):
                        try:
                            b.configure(height=44)
                        except Exception:
                            pass
                    return _on_enter
                def on_leave_factory(b):
                    def _on_leave(_e):
                        try:
                            b.configure(height=40)
                        except Exception:
                            pass
                    return _on_leave
                btn.bind("<Enter>", on_enter_factory(btn))
                btn.bind("<Leave>", on_leave_factory(btn))
            else:
                ttk.Button(button_frame, text=text, command=command).grid(row=0, column=i, padx=4)
        
        # File list with modern styling
        list_frame = ctk.CTkFrame(container, fg_color=self.colors.get("surface", "#2d2d30"), corner_radius=15) if ctk else ttk.LabelFrame(container, text="Files", padding=(5, 5))
        if ctk:
            self.list_frame = list_frame
            # Start with upload view visible; hide library until selected
            list_frame.pack_forget()
        else:
            list_frame.grid(row=2, column=0, sticky=(N, S, E, W), pady=(0, 10))
            container.rowconfigure(2, weight=1)
            list_frame.columnconfigure(0, weight=1)
            list_frame.rowconfigure(0, weight=1)
        
        if ctk:
            list_header = ctk.CTkFrame(list_frame, fg_color="transparent")
            list_header.pack(fill="x", padx=20, pady=(20, 10))
            ctk.CTkLabel(list_header, text="📋 File Library", font=ctk.CTkFont(size=18, weight="bold"), text_color=self.colors.get("text", "#ffffff")).pack(side="left")
            file_count_label = ctk.CTkLabel(list_header, text="0 files", font=ctk.CTkFont(size=12), text_color=self.colors.get("text_secondary", "#cccccc"))
            file_count_label.pack(side="right")
            self.file_count_label = file_count_label
        
        # Treeview with modern styling
        tree_frame = ctk.CTkFrame(list_frame, fg_color="transparent") if ctk else ttk.Frame(list_frame)
        if ctk:
            tree_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        else:
            tree_frame.grid(row=1, column=0, sticky=(N, S, E, W))
            list_frame.columnconfigure(0, weight=1)
            list_frame.rowconfigure(0, weight=1)
        
        columns = ("name", "size", "type", "status", "upload_date")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        
        # Configure treeview styling
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", 
                       background=self.colors.get("background", "#1e1e1e"),
                       foreground=self.colors.get("text", "#ffffff"),
                       fieldbackground=self.colors.get("background", "#1e1e1e"),
                       borderwidth=0,
                       font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                       background=self.colors.get("primary", "#1f6aa5"),
                       foreground=self.colors.get("text", "#ffffff"),
                       font=("Segoe UI", 10, "bold"))
        style.map("Treeview", 
                 background=[("selected", self.colors.get("accent", "#0078d4"))])
        
        self.tree.heading("name", text="📄 File Name")
        self.tree.heading("size", text="📏 Size")
        self.tree.heading("type", text="🏷️ Type")
        self.tree.heading("status", text="🔗 Status")
        self.tree.heading("upload_date", text="📅 Upload Date")
        
        self.tree.column("name", width=350)
        self.tree.column("size", width=100, anchor="center")
        self.tree.column("type", width=150)
        self.tree.column("status", width=100, anchor="center")
        self.tree.column("upload_date", width=150)
        
        v_scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        h_scrollbar = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscroll=v_scrollbar.set, xscroll=h_scrollbar.set)
        
        if ctk:
            self.tree.pack(side="left", fill="both", expand=True)
            v_scrollbar.pack(side="right", fill="y")
            h_scrollbar.pack(side="bottom", fill="x")
        else:
            self.tree.grid(row=0, column=0, sticky=(N, S, E, W))
            v_scrollbar.grid(row=0, column=1, sticky=(N, S))
            h_scrollbar.grid(row=1, column=0, sticky=(E, W))
            tree_frame.columnconfigure(0, weight=1)
            tree_frame.rowconfigure(0, weight=1)
        
        # Instructions with modern styling
        instructions_text = "💾 Upload files to your local vault (SQLite). Files are stored inside the database and persist across restarts."
        if not LOCAL_ONLY:
            instructions_text += "\n☁️ Optionally configure a cloud server in Settings for remote sync."
        
        if ctk:
            self.instructions = ctk.CTkLabel(container, text=instructions_text, 
                                      font=ctk.CTkFont(size=11), 
                                      text_color=self.colors.get("text_secondary", "#cccccc"),
                                      justify="left")
            self.instructions.pack(fill="x", pady=(0, 20), padx=20)
        else:
            instructions = ttk.Label(container, text=instructions_text, font=("Arial", 9))
            instructions.grid(row=3, column=0, sticky=(E, W), pady=(10, 0))

        # Default to Upload view in CTk mode
        if ctk:
            self.show_upload_view()

    def _bind_events(self) -> None:
        self.tree.bind("<<TreeviewSelect>>", self._on_file_select)
        self.search_var.trace_add("write", lambda *_: self.refresh_files())
        self.root.bind("<Return>", lambda event: self.upload_file())

    def _on_file_select(self, event=None) -> None:
        selection = self.tree.selection()
        if selection:
            item = self.tree.item(selection[0])
            values = item['values']
            if values:
                self.status_var.set(f"Selected: {values[0]}")

    def _format_size(self, size_bytes: int) -> str:
        if size_bytes == 0:
            return "0 B"
        size_names = ["B", "KB", "MB", "GB"]
        i = 0
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024.0
            i += 1
        return f"{size_bytes:.1f} {size_names[i]}"

    def refresh_files(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        try:
            files = self.storage.get_local_files()
            search_term = self.search_var.get().lower()
            displayed_count = 0
            
            for file in files:
                if search_term and search_term not in file['local_name'].lower():
                    continue
                
                status = "Synced" if file['is_synced'] else "Local Only"
                size_str = self._format_size(file['file_size'] or 0)
                mime_type = file['file_mime'] or "Unknown"
                
                self.tree.insert("", END, values=(
                    file['local_name'],
                    size_str,
                    mime_type,
                    status,
                    file['upload_date'][:10] if file['upload_date'] else ""
                ))
                displayed_count += 1
            
            # Update file count display
            if hasattr(self, 'file_count_label'):
                self.file_count_label.configure(text=f"{displayed_count} files")
                
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load files: {e}")

    def upload_file(self) -> None:
        file_path = filedialog.askopenfilename(
            title="Select file to upload",
            filetypes=[("All files", "*.*")]
        )
        
        if not file_path:
            return
        
        def upload_worker():
            try:
                self.status_var.set("Uploading...")
                result = self.storage.upload_file(file_path)
                self.root.after(0, lambda: self.status_var.set(f"Uploaded: {result.get('name', 'Unknown')}"))
                self.root.after(0, self.refresh_files)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Upload Error", str(e)))
                self.root.after(0, lambda: self.status_var.set("Upload failed"))
        
        threading.Thread(target=upload_worker, daemon=True).start()

    def download_file(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Download", "Please select a file to download.")
            return
        
        item = self.tree.item(selection[0])
        values = item['values']
        if not values:
            return
        
        file_name = values[0]
        
        # Find the file in database
        files = self.storage.get_local_files()
        selected_file = None
        for file in files:
            if file['local_name'] == file_name:
                selected_file = file
                break
        
        if not selected_file or not selected_file['cloud_id']:
            messagebox.showwarning("Download", "File not found in cloud storage.")
            return
        
        # Auto-save to Downloads folder
        save_path = os.path.join(get_downloads_dir(), file_name)
        # If file already exists, notify and skip download
        if os.path.exists(save_path):
            messagebox.showinfo("Download", f"File already exists in Downloads:\n{save_path}")
            self.status_var.set(f"Already exists: {save_path}")
            return
        
        def download_worker():
            try:
                self.status_var.set("Downloading...")
                self.storage.download_file(selected_file['cloud_id'], save_path)
                self.root.after(0, lambda: self.status_var.set(f"Downloaded to: {save_path}"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Download Error", str(e)))
                self.root.after(0, lambda: self.status_var.set("Download failed"))
        
        threading.Thread(target=download_worker, daemon=True).start()

    def delete_file(self) -> None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Delete", "Please select a file to delete.")
            return
        
        if not messagebox.askyesno("Confirm Delete", "Are you sure you want to delete this file from both local database and cloud storage?"):
            return
        
        item = self.tree.item(selection[0])
        values = item['values']
        if not values:
            return
        
        file_name = values[0]
        
        # Find the file in database
        files = self.storage.get_local_files()
        selected_file = None
        for file in files:
            if file['local_name'] == file_name:
                selected_file = file
                break
        
        if not selected_file:
            return
        
        def delete_worker():
            try:
                self.status_var.set("Deleting...")
                
                # Delete from cloud if it exists there
                if selected_file['cloud_id']:
                    self.storage.delete_cloud_file(selected_file['cloud_id'])
                
                # Remove from local database
                self.storage.remove_local_file(selected_file['id'])
                
                self.root.after(0, lambda: self.status_var.set(f"Deleted: {file_name}"))
                self.root.after(0, self.refresh_files)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Delete Error", str(e)))
                self.root.after(0, lambda: self.status_var.set("Delete failed"))
        
        threading.Thread(target=delete_worker, daemon=True).start()

    def sync_all(self) -> None:
        def sync_worker():
            try:
                self.status_var.set("Syncing...")
                cloud_files = self.storage.get_cloud_files()
                self.root.after(0, lambda: self.status_var.set(f"Found {len(cloud_files)} files in cloud"))
                self.root.after(0, self.refresh_files)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Sync Error", str(e)))
                self.root.after(0, lambda: self.status_var.set("Sync failed"))
        
        threading.Thread(target=sync_worker, daemon=True).start()

    def open_settings(self) -> None:
        """Open settings dialog to configure cloud server"""
        settings_window = (ctk.CTkToplevel(self.root) if ctk else tk.Toplevel(self.root))
        settings_window.title("Cloud Server Settings")
        settings_window.geometry("500x400")
        settings_window.transient(self.root)
        settings_window.grab_set()
        
        # Center the window
        settings_window.geometry("+%d+%d" % (
            self.root.winfo_rootx() + 50,
            self.root.winfo_rooty() + 50
        ))
        
        container = (ctk.CTkFrame(settings_window) if ctk else ttk.Frame(settings_window, padding=20))
        if ctk:
            container.pack(fill=tk.BOTH, expand=True)
        else:
            container.pack(fill=tk.BOTH, expand=True)
        
        # Server URL
        (ctk.CTkLabel(container, text="Cloud Server URL:") if ctk else ttk.Label(container, text="Cloud Server URL:")).grid(row=0, column=0, sticky=W, pady=5)
        url_var = StringVar(value=self.storage.config["cloud_server_url"])
        url_entry = (ctk.CTkEntry(container, textvariable=url_var) if ctk else ttk.Entry(container, textvariable=url_var, width=50))
        url_entry.grid(row=0, column=1, sticky=(E, W), pady=5, padx=(10, 0))
        
        # API Key
        (ctk.CTkLabel(container, text="API Key:") if ctk else ttk.Label(container, text="API Key:")).grid(row=1, column=0, sticky=W, pady=5)
        key_var = StringVar(value=self.storage.config["api_key"])
        key_entry = (ctk.CTkEntry(container, textvariable=key_var, show="*") if ctk else ttk.Entry(container, textvariable=key_var, width=50, show="*"))
        key_entry.grid(row=1, column=1, sticky=(E, W), pady=5, padx=(10, 0))
        
        # Endpoints
        (ctk.CTkLabel(container, text="Upload Endpoint:") if ctk else ttk.Label(container, text="Upload Endpoint:")).grid(row=2, column=0, sticky=W, pady=5)
        upload_var = StringVar(value=self.storage.config["upload_endpoint"])
        upload_entry = (ctk.CTkEntry(container, textvariable=upload_var) if ctk else ttk.Entry(container, textvariable=upload_var, width=50))
        upload_entry.grid(row=2, column=1, sticky=(E, W), pady=5, padx=(10, 0))
        
        (ctk.CTkLabel(container, text="Download Endpoint:") if ctk else ttk.Label(container, text="Download Endpoint:")).grid(row=3, column=0, sticky=W, pady=5)
        download_var = StringVar(value=self.storage.config["download_endpoint"])
        download_entry = (ctk.CTkEntry(container, textvariable=download_var) if ctk else ttk.Entry(container, textvariable=download_var, width=50))
        download_entry.grid(row=3, column=1, sticky=(E, W), pady=5, padx=(10, 0))
        
        (ctk.CTkLabel(container, text="List Endpoint:") if ctk else ttk.Label(container, text="List Endpoint:")).grid(row=4, column=0, sticky=W, pady=5)
        list_var = StringVar(value=self.storage.config["list_endpoint"])
        list_entry = (ctk.CTkEntry(container, textvariable=list_var) if ctk else ttk.Entry(container, textvariable=list_var, width=50))
        list_entry.grid(row=4, column=1, sticky=(E, W), pady=5, padx=(10, 0))
        
        (ctk.CTkLabel(container, text="Delete Endpoint:") if ctk else ttk.Label(container, text="Delete Endpoint:")).grid(row=5, column=0, sticky=W, pady=5)
        delete_var = StringVar(value=self.storage.config["delete_endpoint"])
        delete_entry = (ctk.CTkEntry(container, textvariable=delete_var) if ctk else ttk.Entry(container, textvariable=delete_var, width=50))
        delete_entry.grid(row=5, column=1, sticky=(E, W), pady=5, padx=(10, 0))
        
        # Timeout
        (ctk.CTkLabel(container, text="Timeout (seconds):") if ctk else ttk.Label(container, text="Timeout (seconds):")).grid(row=6, column=0, sticky=W, pady=5)
        timeout_var = StringVar(value=str(self.storage.config["timeout"]))
        timeout_entry = (ctk.CTkEntry(container, textvariable=timeout_var) if ctk else ttk.Entry(container, textvariable=timeout_var, width=50))
        timeout_entry.grid(row=6, column=1, sticky=(E, W), pady=5, padx=(10, 0))
        
        container.columnconfigure(1, weight=1)
        
        # Buttons
        button_frame = ctk.CTkFrame(container) if ctk else ttk.Frame(container)
        button_frame.grid(row=7, column=0, columnspan=2, pady=20)
        
        def test_connection():
            # Temporarily update config for testing
            test_config = {
                "cloud_server_url": url_var.get(),
                "api_key": key_var.get(),
                "upload_endpoint": upload_var.get(),
                "download_endpoint": download_var.get(),
                "list_endpoint": list_var.get(),
                "delete_endpoint": delete_var.get(),
                "timeout": int(timeout_var.get()) if timeout_var.get().isdigit() else 30
            }
            
            # Test connection
            try:
                temp_storage = CloudStorage(self.storage.db_path)
                temp_storage.config = test_config
                if temp_storage.test_connection():
                    messagebox.showinfo("Test", "Connection successful!")
                else:
                    messagebox.showerror("Test", "Connection failed!")
            except Exception as e:
                messagebox.showerror("Test", f"Connection failed: {e}")
        
        def save_settings():
            # Update config
            self.storage.config.update({
                "cloud_server_url": url_var.get(),
                "api_key": key_var.get(),
                "upload_endpoint": upload_var.get(),
                "download_endpoint": download_var.get(),
                "list_endpoint": list_var.get(),
                "delete_endpoint": delete_var.get(),
                "timeout": int(timeout_var.get()) if timeout_var.get().isdigit() else 30
            })
            
            # Save to file
            config_path = os.path.join(os.path.dirname(self.storage.db_path), CONFIG_FILENAME)
            with open(config_path, 'w') as f:
                json.dump(self.storage.config, f, indent=2)
            
            messagebox.showinfo("Settings", "Settings saved successfully!")
            settings_window.destroy()
        
        (ctk.CTkButton(button_frame, text="Test Connection", command=test_connection) if ctk else ttk.Button(button_frame, text="Test Connection", command=test_connection)).pack(side=tk.LEFT, padx=5)
        (ctk.CTkButton(button_frame, text="Save", command=save_settings) if ctk else ttk.Button(button_frame, text="Save", command=save_settings)).pack(side=tk.LEFT, padx=5)
        (ctk.CTkButton(button_frame, text="Cancel", command=settings_window.destroy) if ctk else ttk.Button(button_frame, text="Cancel", command=settings_window.destroy)).pack(side=tk.LEFT, padx=5)


def main() -> int:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(base_dir, DB_FILENAME)
    
    storage = CloudStorage(db_path)
    
    if ctk is None:
        root = tk.Tk()
    else:
        root = ctk.CTk()
    # Set window/app icon if available
    icon_path = os.path.join(base_dir, "cloud_icon.ico")
    if os.path.exists(icon_path):
        try:
            root.iconbitmap(icon_path)
        except Exception:
            pass
    app = CloudFileApp(root, storage)
    
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    
    return 0


if __name__ == "__main__":
    raise SystemExit(main())