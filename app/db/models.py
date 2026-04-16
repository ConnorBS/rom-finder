from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum


class DownloadStatus(str, Enum):
    pending = "pending"
    downloading = "downloading"
    completed = "completed"
    failed = "failed"
    verified = "verified"


class AppSetting(SQLModel, table=True):
    """Key/value store for user-configurable settings."""
    __tablename__ = "app_settings"
    key: str = Field(primary_key=True)
    value: str = ""


class Download(SQLModel, table=True):
    """Tracks items in the download queue."""
    id: Optional[int] = Field(default=None, primary_key=True)
    game_title: str
    system: str
    file_name: str
    file_path: Optional[str] = None
    source_url: str
    source_id: str = "archive_org"
    archive_identifier: str = ""
    status: DownloadStatus = DownloadStatus.pending
    progress: float = 0.0
    file_hash: Optional[str] = None
    hash_verified: bool = False
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class LibraryEntry(SQLModel, table=True):
    """ROMs that have been downloaded and are tracked locally."""
    __tablename__ = "library"
    id: Optional[int] = Field(default=None, primary_key=True)
    game_title: str
    system: str
    file_name: str
    file_path: str
    file_hash: Optional[str] = None
    hash_verified: bool = False
    ra_game_id: Optional[int] = None
    ra_matched: bool = False
    added_at: datetime = Field(default_factory=datetime.utcnow)
