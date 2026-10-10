"""Fill a scratch database for UI checks (never the owner's data).

Usage (bash, inside `fin-doc`), with a scratch folder S:

    export APP_ENV=development DATABASE_URL="sqlite:///$S/app.db" SUPABASE_URL="" \
           LOCAL_STORAGE_DIR="$S/storage" LOG_FILE="$S/app.log" ADMIN_USERNAME=admin ADMIN_INITIAL_PASSWORD=Admin1234
    python scripts/seed_scratch.py "$S"
    python -m streamlit run app.py --server.port 8511 --server.headless true

Creates admin/Admin1234, analyst ali/Analyst123 and viewer viw/Viewer1234 (no forced password change),
a mock provider with two models and one key, and two finished requests: 1395–1399 + a failing file with
model 1, 1400 + a second 1399 document with model 2 (a company-year duplicate). The first request is
shared with the viewer. Afterwards the provider is slowed to 8 s per call, so a request started in the
browser can be stopped. Delete any `data/spool/<request>` folder the scratch server creates.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import update  # noqa: E402

from core.db import session_scope  # noqa: E402
from core.migrate import upgrade_database  # noqa: E402
from core.models import Provider, Role, User  # noqa: E402
from services import history, jobs  # noqa: E402
from services import providers as pv  # noqa: E402
from services.auth import ensure_bootstrap_admin, load_active_user  # noqa: E402
from services.uploads import IncomingFile  # noqa: E402
from services.users import create_user, default_form  # noqa: E402


def main(scratch: Path) -> None:
    upgrade_database()
    if error := ensure_bootstrap_admin():
        raise SystemExit(error)

    def user(name: str, role: Role, password: str) -> int:
        form = default_form(role)
        form.display_name = name
        return create_user(1, name, form, password)[0].id

    ali = user("ali", Role.ANALYST, "Analyst123")
    viw = user("viw", Role.VIEWER, "Viewer1234")
    with session_scope() as db:
        db.execute(update(User).values(must_change_password=False))

    provider = pv.save_provider(None, pv.ProviderForm("آزمایشی", "mock",
                                                      "mock://?delay=0&fail_names=bad&extra_names=1399"))
    pv.save_model(None, provider.id, pv.ModelForm("mock-1", "مدل آزمایشی", max_attempts=1))
    pv.save_model(None, provider.id, pv.ModelForm("mock-2", "مدل دوم", max_attempts=1))
    pv.save_key(None, provider.id, pv.KeyForm("کلید ۱", "secret-key-0001"))
    pv.set_grant(pv.Subject(role=Role.ANALYST), provider.id, "allow", None, None)

    manager = jobs.JobManager(spool_dir=scratch / "spool", backoff=(0,))
    analyst = load_active_user(ali)
    choices = {c.model_name: c for c in pv.usable_models(ali)}

    def doc(name: str) -> IncomingFile:
        return IncomingFile(name, f"%PDF-1.4 {name}".encode())

    def run(model: str, names: list[str]) -> str:
        choice = choices[model]
        request_id = manager.submit(analyst, choice, [k.id for k in choice.keys], [doc(n) for n in names])
        manager.wait_idle(request_id, 60)
        return request_id

    first = run("mock-1", [f"{y}.pdf" for y in range(1395, 1400)] + ["bad.pdf"])
    second = run("mock-2", ["1400.pdf", "1399-b.pdf"])
    history.set_shares(analyst, first, [viw])
    manager.close()
    with session_scope() as db:  # slow calls from now on, so a browser test can stop a running request
        for p in db.query(Provider):
            p.base_url = "mock://?delay=8&fail_names=bad&extra_names=1399"
    print("seeded", first, second)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/seed_scratch.py <scratch folder>")
    main(Path(sys.argv[1]))
