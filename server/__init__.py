"""TigerDuck push notification server.

Runs on the Mac mini behind nginx-proxy-manager + Cloudflare, serves the iOS app
at https://api.tigerduck.app/v2/. Separate from backend/api/ which is POC-only.
"""

# Single source of truth for the backend version. Surfaced via:
#   * FastAPI's OpenAPI info.version (see main.create_app)
#   * The /version + /{api_base_path}/version endpoints
#   * _compose-files.sh's print_stack_status (greps this file directly)
#   * The portal's status page (hits /version on the backend)
__version__ = "2.1.0"

from server import config

__all__ = ["__version__", "config"]
