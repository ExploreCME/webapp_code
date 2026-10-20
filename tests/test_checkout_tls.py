import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CheckoutTLSRuntimeTests(unittest.TestCase):
    def test_gevent_worker_initialization_supports_stripe_tls(self):
        self.assert_stripe_tls_startup(preload=False)

    def test_preload_env_override_supports_stripe_tls(self):
        self.assert_stripe_tls_startup(preload=True)

    def assert_stripe_tls_startup(self, preload):
        # Isolate monkey-patching from the test runner and other test modules.
        env = dict(os.environ, GUNICORN_CMD_ARGS="--preload" if preload else "")
        result = subprocess.run(
            [sys.executable, "-c", """
import os
import sys
from unittest.mock import patch
from gunicorn.app.wsgiapp import WSGIApplication
from gunicorn.arbiter import Arbiter

sys.argv = ["gunicorn", "-c", "gunicorn.conf.py", "display_database:app"]
with patch.dict(os.environ, {"FLASK_ENV": "testing"}), patch("os.makedirs"):
    application = WSGIApplication()
    assert application.cfg.worker_class_str == "gevent"
    assert application.cfg.preload_app == (os.environ["GUNICORN_CMD_ARGS"] == "--preload")
    # Config loading must patch SSL even before Arbiter can preload the app.
    from gevent import monkey
    assert monkey.is_module_patched("ssl")
    assert "display_database" not in sys.modules
    # Arbiter setup preloads the WSGI app if enabled, before worker patching.
    arbiter = Arbiter(application)
    assert ("display_database" in sys.modules) == application.cfg.preload_app
    worker = application.cfg.worker_class(
        0, os.getpid(), [], application, 30, application.cfg, arbiter.log,
    )
    worker.patch()
    application.wsgi()

import ssl
from gevent import monkey
from gevent.server import StreamServer
from urllib3.util.ssl_ import create_urllib3_context
import requests
import stripe

assert monkey.is_module_patched("ssl")
context = create_urllib3_context()
assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
assert context.verify_mode == ssl.CERT_REQUIRED
assert context.check_hostname

# Reach the real Stripe -> requests -> urllib3 TLS path without credentials,
# external services, or creating a checkout. The peer sends an invalid TLS reply.
def close_connection(sock, address):
    sock.recv(4096)
    sock.sendall(b"HTTP/1.1 400 Bad Request\\r\\n\\r\\n")
    sock.close()

server = StreamServer(("127.0.0.1", 0), close_connection)
server.start()
http_session = requests.Session()
http_session.trust_env = False
try:
    client = stripe.RequestsClient(timeout=2, session=http_session)
    try:
        client.request(
            "post", f"https://127.0.0.1:{server.server_port}/v1/checkout/sessions",
            headers={}, post_data="",
        )
    except stripe.APIConnectionError as error:
        assert "RecursionError" not in str(error), str(error)
        assert "maximum recursion depth" not in str(error), str(error)
        assert "SSLError" in str(error), str(error)
    else:
        raise AssertionError("Expected a normal TLS connection error")
finally:
    http_session.close()
    server.stop()
"""],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("MonkeyPatchWarning", result.stderr)


if __name__ == "__main__":
    unittest.main()
