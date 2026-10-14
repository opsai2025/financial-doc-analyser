"""Start the app the way Posit Connect Cloud does: `app.py` as the page script, without `st.App` routes.

Posit calls Streamlit's bootstrap directly, so `streamlit run`'s st.App discovery never happens (D-15.8):
`app.py` then runs `streamlit_app.py` itself and login uses the Posit workaround (D-15.9).
Use it with the scratch environment from CLAUDE.md, from the project root:

    python scripts/run_like_posit.py 8513

Check: `curl localhost:8513/auth/ping` returns Streamlit's HTML page (no routes), while
`streamlit run app.py` on another port returns "fda-auth-ok".
"""

import sys

from streamlit.web import bootstrap

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8513
options = {"server.port": port, "server.headless": True}
bootstrap.load_config_options(flag_options=options)
bootstrap.run("app.py", False, [], options)
