"""Public demo entry point for Streamlit Community Cloud (D62).

Community Cloud runs the file named in the app's "Main file path" from the
repository root. This entry point always enables demo mode, so the public app
can never open a real raw store or show live issuer holdings: it builds the
committed synthetic fixtures into a temporary directory and shows the
"Demo data — synthetic funds" banner.

Use ``streamlit run etfh/dashboard/app.py`` for the local dashboard against a
real build (``ETFH_DB``), and ``ETFH_DEMO=1 streamlit run
etfh/dashboard/app.py`` to preview the demo locally.
"""

from etfh.dashboard.app import main

main(force_demo=True)
