"""Tiny HTTP health check server for Docker and monitoring.

Runs in a background thread. Exposes a single endpoint:

    GET /health → 200 with JSON status

The response includes the last run status from the `runs` table so
monitoring can detect if the service is running but failing.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from mailsort.db.database import Database

logger = logging.getLogger(__name__)

_DEFAULT_PORT = 8025


class _HealthHandler(BaseHTTPRequestHandler):
    """Handler for /health endpoint."""

    db_path: str = ""

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return

        status = _get_status(self.db_path)
        body = json.dumps(status).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Suppress default stderr logging — use our logger instead
        logger.debug("Health check: %s", format % args)


def _get_status(db_path: str) -> dict:
    """Query the most recent run for health status."""
    status: dict = {"ok": True, "service": "mailsort"}
    try:
        db = Database(db_path)
        db.connect()
        row = db.execute(
            "SELECT run_id, status, started_at, finished_at, emails_seen, emails_moved, error_summary "
            "FROM runs ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        db.close()

        if row:
            status["last_run"] = {
                "run_id": row["run_id"][:8],
                "status": row["status"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "emails_seen": row["emails_seen"],
                "emails_moved": row["emails_moved"],
            }
            if row["status"] == "failed":
                status["ok"] = False
                status["error"] = row["error_summary"]
        else:
            status["last_run"] = None
    except Exception as e:
        status["ok"] = False
        status["error"] = str(e)

    return status


def start_health_server(db_path: str, port: int = _DEFAULT_PORT) -> Optional[HTTPServer]:
    """Start the health check server in a daemon thread. Returns the server instance."""
    _HealthHandler.db_path = db_path

    try:
        server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    except OSError as e:
        logger.warning("Could not start health server on port %d: %s", port, e)
        return None

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check server listening on port %d", port)
    return server
