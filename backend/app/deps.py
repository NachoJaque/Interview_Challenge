from typing import Annotated

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.database import get_db
from app.models import User
from app.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{get_settings().root_path}/auth/login")

# Same scheme, but a missing token is not an error: for public endpoints that
# show more to some callers.
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{get_settings().root_path}/auth/login", auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
# Path ids are bounded to PostgreSQL's INTEGER range so out-of-range values
# are rejected with 422 instead of reaching the database.
ResourceId = Annotated[int, Path(gt=0, le=2_147_483_647)]


def get_current_user(db: DbSession, token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    user_id = decode_access_token(token)
    user = db.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(db: DbSession, token: Annotated[str | None, Depends(optional_oauth2_scheme)]) -> User | None:
    """The caller if they sent a valid token, otherwise None (anonymous)."""
    user_id = decode_access_token(token) if token else None
    return db.get(User, user_id) if user_id is not None else None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def get_current_admin(user: CurrentUser) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    return user


CurrentAdmin = Annotated[User, Depends(get_current_admin)]
