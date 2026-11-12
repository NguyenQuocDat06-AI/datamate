"""Kaggle API for account 0 (KAGGLE_USERNAME/KAGGLE_KEY) or 1 (KAGGLE_USERNAME1/KAGGLE_KEY1) from the project .env.
Never prints credentials. kernel_id(account, slug) builds '<user>/<slug>' for that account."""
import os
from dotenv import dotenv_values

ENV = "/home/ubuntu/datamate/source_test/.env"


def _creds(account: int):
    v = dotenv_values(ENV)
    suffix = "" if account == 0 else str(account)
    user, key = v.get("KAGGLE_USERNAME" + suffix), v.get("KAGGLE_KEY" + suffix) or v.get("KAGGLE_API_TOKEN" + suffix)
    if not user or not key:
        raise SystemExit(f"account {account}: credentials missing in .env")
    return user, key


def api(account: int = 0):
    """Authenticate as exactly this account: a private config dir holding only its kaggle.json, so a stray
    ~/.kaggle/access_token or kaggle.json of another account can never take precedence."""
    import json
    import tempfile
    user, key = _creds(account)
    conf = tempfile.mkdtemp(prefix=f"kaggle_acc{account}_")
    os.chmod(conf, 0o700)
    path = os.path.join(conf, "kaggle.json")
    with open(path, "w") as fh:
        json.dump({"username": user, "key": key}, fh)
    os.chmod(path, 0o600)
    os.environ["KAGGLE_CONFIG_DIR"] = conf
    os.environ["KAGGLE_USERNAME"], os.environ["KAGGLE_KEY"] = user, key
    os.environ.pop("KAGGLE_API_V1_TOKEN_PATH", None)  # checked before KAGGLE_API_TOKEN by kagglesdk
    if key.startswith("KGAT_"):
        # A new-style API token: kagglesdk takes KAGGLE_API_TOKEN before ~/.kaggle/access_token.
        os.environ["KAGGLE_API_TOKEN"] = key
    else:
        os.environ.pop("KAGGLE_API_TOKEN", None)
    from kaggle.api.kaggle_api_extended import KaggleApi
    a = KaggleApi()
    a.authenticate()
    return a


def kernel_id(account: int, slug: str) -> str:
    return f"{_creds(account)[0]}/{slug}"
