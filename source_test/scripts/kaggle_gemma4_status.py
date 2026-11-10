"""Query Kaggle status for bizreason-gemma4-12b-sql-eval1000 and download outputs when complete."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values


def get_authenticated_api():
    # Resolve .env path silently
    script_dir = Path(__file__).resolve().parent
    root_dir = script_dir.parent
    env_file = root_dir / ".env"

    if env_file.exists():
        env_vars = dotenv_values(str(env_file))
        key = env_vars.get("KAGGLE_KEY") or env_vars.get("KAGGLE_API_TOKEN")
        if env_vars.get("KAGGLE_USERNAME"):
            os.environ["KAGGLE_USERNAME"] = env_vars["KAGGLE_USERNAME"]
        if key:
            os.environ["KAGGLE_KEY"] = key
            os.environ["KAGGLE_API_TOKEN"] = key

    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    if "KAGGLE_KEY" in os.environ:
        api.config_values[api.CONFIG_NAME_TOKEN] = os.environ["KAGGLE_KEY"]
    return api


def main():
    parser = argparse.ArgumentParser(description="Query status and download Kaggle kernel results.")
    parser.add_argument("--watch", action="store_true", help="Continuously poll until complete or failed")
    parser.add_argument("--download", action="store_true", help="Download output files regardless of status")
    parser.add_argument("--output-dir", default=None, help="Output destination folder")
    parser.add_argument("--interval", type=int, default=30, help="Polling interval in seconds (default: 30)")
    args = parser.parse_args()

    api = get_authenticated_api()
    username = api.get_config_value(api.CONFIG_NAME_USER)
    kernel_slug = f"{username}/bizreason-gemma4-12b-sql-eval1000"

    root_dir = Path(__file__).resolve().parent.parent
    dest_dir = Path(args.output_dir) if args.output_dir else (root_dir / "artifacts" / "kaggle_eval_1000_gemma4_12b_sql")
    dest_dir.mkdir(parents=True, exist_ok=True)

    print(f"Target Kernel: {kernel_slug}")
    print(f"Destination:   {dest_dir}")

    while True:
        try:
            status_resp = api.kernels_status(kernel_slug)
            status = getattr(status_resp, "status", None) or str(status_resp)
            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Kernel Status: {status}")
        except Exception as e:
            print(f"Failed to query kernel status: {e}")
            status = "error"

        norm_status = str(status).strip().lower()

        if norm_status in ("complete", "completed") or args.download:
            print(f"Downloading kernel output files to {dest_dir}...")
            try:
                api.kernels_output(kernel_slug, path=str(dest_dir))
                print(f"Download complete! Files saved to {dest_dir}")
                for f in dest_dir.iterdir():
                    print(f"  - {f.name} ({f.stat().st_size / 1024:.1f} KB)")
            except Exception as e:
                print(f"Failed to download kernel output: {e}")
            break

        if norm_status in ("failed", "error", "cancelled"):
            print(f"Kernel execution halted with status: {status}")
            sys.exit(1)

        if not args.watch:
            break

        time.sleep(args.interval)


if __name__ == "__main__":
    main()

