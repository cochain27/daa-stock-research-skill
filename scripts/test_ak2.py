import sys
VENV = "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages"
print("venv:", VENV)
print("exists:", __import__('os').path.exists(VENV))
print("akshare there:", __import__('os').path.exists(VENV + "/akshare"))
sys.path.insert(0, VENV)
print("path[0]:", sys.path[0])
import ak
print("OK", ak.__version__)
