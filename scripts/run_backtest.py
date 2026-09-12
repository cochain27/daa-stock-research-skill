#!/Users/chenyuting/.workbuddy/binaries/python/versions/3.13.12/bin/python3
"""run_backtest.py"""
import os, sys
for k in list(os.environ):
    if "proxy" in k.lower():
        del os.environ[k]

VENV = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
sys.path.insert(0, VENV)

sys.path.insert(0, "/Users/chenyuting/Documents/workbuddy/workbuddy-daa/daa-stock-research-skill/scripts")
import backtest_engine
sys.exit(backtest_engine.main())
