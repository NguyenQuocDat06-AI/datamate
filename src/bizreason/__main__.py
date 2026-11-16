"""`python -m bizreason` runs the V2 pipeline. The legacy free-SQL pipeline is only at `python -m bizreason.cli`."""

from bizreason import run_v2

if __name__ == "__main__":
    raise SystemExit(run_v2.main())
