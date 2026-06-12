#!/usr/bin/env python3
"""Static file server that serves WebP (and friends) with correct MIME types.

Python's stdlib http.server guesses Content-Type from `mimetypes`, which on
some systems/versions does not know `.webp` -- so it sends
application/octet-stream and the browser DOWNLOADS the image instead of showing
it inline. Registering the types first fixes that.

    python3 serve.py [port] [directory]   # default 8000 in the current dir
"""
import http.server
import mimetypes
import sys

for ext, ctype in {
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}.items():
    mimetypes.add_type(ctype, ext)

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
directory = sys.argv[2] if len(sys.argv) > 2 else "."


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=directory, **kw)


print(f"serving {directory} at http://localhost:{port}  (webp -> image/webp)")
http.server.ThreadingHTTPServer(("", port), Handler).serve_forever()
