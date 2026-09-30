"""Admin CLI (run inside the container, e.g. `docker compose run --rm web python -m app.cli ...`):

  create-user  --email a@b.c --name "Ann" --role admin
  set-password --email a@b.c      # recovery when no admin can log in; also unlocks
"""

import argparse
import getpass
import sys

from sqlalchemy import delete, select

from app.db import session_scope
from app.models import LoginFailure, User, UserRole, UserSession
from app.security import hash_password


def create_user(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("Password: ")
    if len(password) < 10:
        print("Password must be at least 10 characters", file=sys.stderr)
        return 1
    with session_scope() as db:
        if db.scalar(select(User).where(User.email == args.email.strip().lower())):
            print(f"User {args.email} already exists", file=sys.stderr)
            return 1
        db.add(
            User(
                email=args.email,
                full_name=args.name,
                role=UserRole(args.role),
                password_hash=hash_password(password),
            )
        )
    print(f"Created {args.role} {args.email}")
    return 0


def set_password(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("New password: ")
    if len(password) < 10:
        print("Password must be at least 10 characters", file=sys.stderr)
        return 1
    email = args.email.strip().lower()
    with session_scope() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"No user {email}", file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        user.is_active = True
        db.execute(delete(UserSession).where(UserSession.user_id == user.id))
        db.execute(delete(LoginFailure).where(LoginFailure.email == email))
    print(f"Password set for {email}; sessions revoked, login failures cleared")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(required=True)
    p = sub.add_parser("create-user", help="create an admin or agent account")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", choices=[r.value for r in UserRole], default=UserRole.AGENT.value)
    p.add_argument("--password", help="omit to be prompted (avoids shell history)")
    p.set_defaults(func=create_user)
    p = sub.add_parser("set-password", help="reset a password and unlock/reactivate the account")
    p.add_argument("--email", required=True)
    p.add_argument("--password", help="omit to be prompted (avoids shell history)")
    p.set_defaults(func=set_password)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
