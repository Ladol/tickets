"""
Loads settings into os.environ before the bot starts.

1. A local `.env` file, if present (local development).
2. If COMBOIO_SECRET is set (e.g. in the VM's systemd unit), a dotenv-formatted Google Secret
   Manager secret, read with the VM service account's token from the metadata server. This way
   the credentials never touch the VM's disk. See README for the one-time setup.

COMBOIO_SECRET is a resource name: projects/<project>/secrets/<name>[/versions/<version>]
"""
import base64
import io
import os
import time
import requests
from dotenv import load_dotenv, dotenv_values

METADATA_TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"


def fetch_secret(resource: str) -> str:
    if "/versions/" not in resource:
        resource += "/versions/latest"

    token_res = requests.get(METADATA_TOKEN_URL, headers={"Metadata-Flavor": "Google"}, timeout=5)
    token_res.raise_for_status()
    token = token_res.json()["access_token"]

    res = requests.get(
        f"https://secretmanager.googleapis.com/v1/{resource}:access",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    res.raise_for_status()
    return base64.b64decode(res.json()["payload"]["data"]).decode("utf-8")


def load_config():
    load_dotenv()

    resource = os.getenv("COMBOIO_SECRET")
    if not resource:
        return

    # The metadata server / network can be slow right after the VM boots.
    for attempt in range(1, 6):
        try:
            content = fetch_secret(resource)
            break
        except Exception as e:
            if attempt == 5:
                raise RuntimeError(f"Could not read {resource} from Secret Manager: {e}") from e
            print(f"[Config] Secret Manager not reachable yet ({e}); retrying...")
            time.sleep(attempt * 3)

    values = dotenv_values(stream=io.StringIO(content))
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    print(f"[Config] Loaded {len(values)} settings from Secret Manager.")
