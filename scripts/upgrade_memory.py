"""预演或执行离线记忆升级。"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from memory_upgrade import upgrade

parser = argparse.ArgumentParser()
parser.add_argument("--apply", action="store_true")
args = parser.parse_args()
print(upgrade(Path(__file__).resolve().parents[1], apply=args.apply))
