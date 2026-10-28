"""Public demo entry point for Streamlit Community Cloud (D62).

Community Cloud runs the file named in the app's "Main file path" from the
repository root. This entry point always enables demo mode, so the public app
can never open a real raw store or show live issuer holdings: it builds the
committed synthetic fixtures into a temporary directory and shows the
"Demo data — synthetic funds" banner. It needs no secrets and no environment
variables.

Community Cloud deploy settings (documented from the current Community Cloud
docs, 2026-10-28):

- repository ``broiisapro/cad-etf-holdings``, branch ``main``;
- main file path ``streamlit_app.py`` (repository root);
- Python version 3.12, chosen in "Advanced settings" (3.12 is Cloud's default
  and the version ``requires-python`` and ``uv.lock`` pin);
- secrets: none.

Community Cloud installs the app's dependencies from the first dependency file
it finds, searching the entry point's directory and then the repository root,
in this order: ``uv.lock``, ``Pipfile``, ``environment.yml``,
``requirements.txt``, ``pyproject.toml``. This repository commits ``uv.lock``
next to this file, so Cloud syncs the locked uv project (which also installs
the ``etfh`` package) instead of pip-installing a hand-written list. No
``requirements.txt`` or ``packages.txt`` is committed: the dependencies are
pure wheels (``opencv-python-headless``, ``pypdfium2``, ``fastexcel``), so no
``apt-get`` package is needed, and nothing writes outside the temporary
directory Cloud provides.

Use ``streamlit run etfh/dashboard/app.py`` for the local dashboard against a
real build (``ETFH_DB``), and ``ETFH_DEMO=1 streamlit run
etfh/dashboard/app.py`` to preview the demo locally.
"""

from etfh.dashboard.app import main

main(force_demo=True)
