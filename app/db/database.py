from sqlmodel import create_engine, Session
from app.config import get_settings

settings = get_settings()
engine = create_engine(settings.db_url, echo=settings.debug)


def get_session():
    with Session(engine) as session:
        yield session
