"""The allowlist, from the command line. This is the only way an account is created.

    python admin_users.py allow you@example.com --label "MK" --admin
    python admin_users.py list
    python admin_users.py revoke someone@example.com
    python admin_users.py purge someone@example.com          # forget them entirely
    python admin_users.py sessions --email you@example.com
    python admin_users.py kill you@example.com               # sign them out everywhere
    python admin_users.py audit --limit 30
    python admin_users.py summary

Deliberately a CLI and not an HTTP endpoint. Adding a user is the one action with no
recovery path if it is done by the wrong person, and requiring shell access to the box
means the blast radius of any bug in the web layer stops short of the allowlist.

`revoke` deactivates and signs the account out; `purge` deletes it. Prefer `revoke`: the
audit trail keeps naming an address the `users` table can still explain.
"""

from __future__ import annotations

import argparse

import authdb


def _print_table(rows: list[dict], cols: list[tuple[str, str]]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [max(len(head), *(len(str(r.get(key) or "-")) for r in rows))
              for key, head in cols]
    print("  ".join(h.ljust(w) for (_, h), w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(str(r.get(k) or "-").ljust(w) for (k, _), w in zip(cols, widths)))


def cmd_allow(args) -> None:
    user = authdb.allow(args.email, label=args.label, is_admin=args.admin)
    authdb.audit("user.allowed", user["email"], detail=f"admin={bool(args.admin)}")
    role = "admin" if user["is_admin"] else "user"
    print(f"allowed {user['email']}  ({role})")
    print("They can sign in now: POST /auth/otp with that address.")


def cmd_link(args) -> None:
    """Point one address at another's account, so both sign in to one book.

    Guarded on what the SOURCE account already owns. Linking rewrites which account an
    address resolves to, so any strategy, order or fill recorded under its old id becomes
    unreachable — the desk keys on the id and has no way to know the person behind it
    moved. Refusing here beats explaining an orphaned track record later.
    """
    src = authdb.user(authdb.normalize_email(args.email))
    if src is None:
        raise SystemExit(f"{args.email} is not on the allowlist")

    old = src["account_id"]
    owned = []
    if old:
        try:
            from stockhunt import deskdb
            owned = deskdb.registrations(old)
        except Exception as exc:                    # the desk ledger may not exist yet
            print(f"  (could not check the desk ledger: {exc})")

    if owned and not args.force:
        raise SystemExit(
            f"{args.email} is account {old} and already owns {len(owned)} "
            f"strateg{'y' if len(owned) == 1 else 'ies'}:\n" +
            "\n".join(f"    {r['strategy_id']}  {r['state']}" for r in owned) +
            f"\n\nLinking would leave those under {old}, which nothing can reach any "
            f"more. Retire them first, or pass --force if you know they are disposable.")

    account = authdb.link_account(args.email, args.to)
    print(f"linked {authdb.normalize_email(args.email)} -> account {account}")
    print(f"shared with: {', '.join(authdb.emails_for_account(account))}")
    print("Both addresses now sign in to the same strategies, orders and record.")


def cmd_revoke(args) -> None:
    if not authdb.revoke(args.email):
        raise SystemExit(f"{args.email} is not on the allowlist")
    authdb.audit("user.revoked", args.email)
    print(f"revoked {authdb.normalize_email(args.email)} and signed out every session")


def cmd_purge(args) -> None:
    email = authdb.normalize_email(args.email)
    if not args.yes:
        reply = input(f"delete {email}, its sessions and its codes? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            raise SystemExit("cancelled")
    if not authdb.purge(email):
        raise SystemExit(f"{email} is not on the allowlist")
    authdb.audit("user.purged", email)
    print(f"purged {email}. The audit trail still has their history.")


def cmd_list(_args) -> None:
    rows = authdb.users()
    for r in rows:
        r["state"] = "active" if r["active"] else "revoked"
        r["role"] = "admin" if r["is_admin"] else "user"
        r["sessions"] = len(authdb.sessions_for(r["email"]))
    _print_table(rows, [("email", "EMAIL"), ("label", "LABEL"), ("role", "ROLE"),
                        ("state", "STATE"), ("sessions", "LIVE"),
                        ("last_login_at", "LAST LOGIN")])


def cmd_sessions(args) -> None:
    if args.email:
        rows = authdb.sessions_for(args.email)
    else:
        rows = [s for u in authdb.users() for s in authdb.sessions_for(u["email"])]
    _print_table(rows, [("email", "EMAIL"), ("created_at", "CREATED"),
                        ("last_used_at", "LAST USED"), ("expires_at", "EXPIRES"),
                        ("ip", "IP")])


def cmd_kill(args) -> None:
    n = authdb.revoke_sessions(args.email)
    authdb.audit("user.sessions_killed", args.email, detail=f"{n} revoked")
    print(f"revoked {n} session(s) for {authdb.normalize_email(args.email)}")


def cmd_audit(args) -> None:
    _print_table(authdb.recent_audit(args.limit, args.email),
                 [("ts", "WHEN"), ("event", "EVENT"), ("email", "EMAIL"),
                  ("ip", "IP"), ("detail", "DETAIL")])


def cmd_summary(_args) -> None:
    for key, value in authdb.summary().items():
        print(f"{key:>14}  {value}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("allow", help="put an email on the allowlist (or reactivate it)")
    p.add_argument("email")
    p.add_argument("--label", help="who this is, for the audit trail")
    p.add_argument("--admin", action="store_true")
    p.set_defaults(func=cmd_allow)

    p = sub.add_parser("link", help="make two addresses sign in to ONE account")
    p.add_argument("email", help="the address to move")
    p.add_argument("to", help="the address whose account it should join")
    p.add_argument("--force", action="store_true",
                   help="link even if the moving address already owns strategies, "
                        "which will leave them unreachable")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("revoke", help="deactivate an account and sign it out")
    p.add_argument("email")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("purge", help="delete an account outright")
    p.add_argument("email")
    p.add_argument("--yes", action="store_true", help="skip the confirmation")
    p.set_defaults(func=cmd_purge)

    sub.add_parser("list", help="the allowlist").set_defaults(func=cmd_list)

    p = sub.add_parser("sessions", help="live tokens")
    p.add_argument("--email")
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("kill", help="revoke every token on one account")
    p.add_argument("email")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("audit", help="the auth event log, newest first")
    p.add_argument("--email")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_audit)

    sub.add_parser("summary", help="counts").set_defaults(func=cmd_summary)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
