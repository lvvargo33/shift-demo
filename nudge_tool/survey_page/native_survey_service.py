"""Narrow production HTTP service behind a trusted TLS-terminating proxy."""
from http.server import ThreadingHTTPServer
from threading import BoundedSemaphore

from .native_survey_http import SurveyHandler
from .native_survey_production import ProductionConfig, ProductionRepository


class ProductionHandler(SurveyHandler):
    server_version = "Survey"
    sys_version = ""

    def _host_ok(self):
        # The ingress must replace X-Forwarded-Proto, preserve Host, and prevent
        # direct public access to this listener. Never consult forwarded Host.
        return (self.headers.get_all("Host") == [self.server.config.public_host]
                and self.headers.get_all("X-Forwarded-Proto") == ["https"])

    def _origin(self):
        return self.server.config.public_origin

    def do_GET(self):
        if self.path == "/healthz":
            # Platform health probes can use internal HTTP/Host. No storage read.
            return self._reply(200, {"status": "ok"})
        return super().do_GET()

    def do_POST(self):
        if (self.headers.get_all("Origin") != [self._origin()]
                or self.headers.get("Transfer-Encoding") is not None
                or len(self.headers.get_all("Content-Length", [])) != 1
                or len(self.headers.get_all("Authorization", [])) != 1):
            return self._reply(403, {"error": "request"})
        return super().do_POST()

    def send_error(self, code, message=None, explain=None):
        self._reply(code, {"error": "request"})


class ProductionServer(ThreadingHTTPServer):
    """Bounded connections and read timeout; ingress supplies TLS/rate limits."""
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, address, config, repository):
        if (not isinstance(config, ProductionConfig)
                or not isinstance(repository, ProductionRepository)
                or repository._context.gym_id != config.gym_id):
            raise ValueError("fixed production configuration/repository required")
        self.config = config
        self.repository = repository
        self.support_email = config.support_email
        self._slots = BoundedSemaphore(64)
        super().__init__(address, ProductionHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address

    def process_request(self, request, address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self._slots.release()

    def handle_error(self, request, address):
        pass  # Never dump request/context/token or SDK details into public logs.
