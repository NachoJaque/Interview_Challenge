from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.audit import audit
from app.deps import CurrentUser, DbSession
from app.models import User
from app.schemas import Token, UserCreate, UserRead
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# Checked when the email is unknown, so a failed login takes the same time
# whether or not the account exists (no user enumeration through timing).
_DUMMY_HASH = hash_password("not-a-real-password")

_EMAIL_TAKEN = HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: DbSession) -> User:
    # Emails are always stored in lower case, so plain equality uses the unique index.
    email = payload.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise _EMAIL_TAKEN
    user = User(email=email, full_name=payload.full_name, hashed_password=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        # Two registrations for the same email raced; the unique index picked the winner.
        db.rollback()
        raise _EMAIL_TAKEN from None
    audit("user_registered", user_id=user.id)
    return user


@router.post("/login", response_model=Token)
def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], db: DbSession) -> Token:
    """OAuth2 password flow: send `username` (the email) and `password` as form data."""
    user = db.scalar(select(User).where(User.email == form.username.strip().lower()))
    password_ok = verify_password(form.password, user.hashed_password if user else _DUMMY_HASH)
    if user is None or not password_ok:
        # The attempted email is deliberately not logged (it is personal data).
        audit("login_failed", user_id=user.id if user else None)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    audit("login_succeeded", user_id=user.id)
    return Token(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser) -> User:
    return user
