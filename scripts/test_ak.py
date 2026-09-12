#!/usr/bin/env python3
import sys
sys.path.insert(0, "/Users/chenyuting/.workbuddy/binaries/python/envs/default/lib/python3.13/site-packages")
print("PATH:", sys.path[:4])
import ak
print("OK", ak.__version__)
