"""Storyteller web app: a backend + reader UI on top of the illustration pipeline.

Books, pages, roster sheets, and lazily-generated scene images all live in one
SQLite database (see ``db.py``). The heavy preprocessing (extract -> registry ->
roster sheets -> page segmentation) runs as a subprocess (``process.py``) so it
can reuse the env-driven pipeline as-is; per-page scene images are generated
lazily and prefetched while reading (``scene.py`` + ``server.py``).
"""
