"""重建两个人的口味画像（tag 版）。后台跑，约 40 分钟。

用法：python scripts/build_taste.py [预算条数]
"""

import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from bili import config, taste
from bili.client import Client
from bili.store import read_json

BUDGET = int(sys.argv[1]) if len(sys.argv) > 1 else 1000

cfg = config.load()
cookies = (read_json(cfg.paths.state / "cookies.json", {}) or {}).get("cookies") or {}
client = Client(cookies)

print(f"预算 {BUDGET} 条/人，开始", flush=True)
started = time.time()

bg = taste.background(client)
print(f"背景语料 tag：{len(bg)} 个", flush=True)

for uid, role in ((cfg.targets.admin_uid, "管理员"),):
    print(f"--- {role} uid={uid} ---", flush=True)
    profile = taste.build(cfg, client, uid, budget=BUDGET, bg=bg)
    top = list((profile.get("tokens") or {}).items())[:15]
    print(
        f"{role}: 采样 {profile.get('sampled_items')} 条 → {len(profile.get('tokens') or {})} 个 tag",
        flush=True,
    )
    print("  Top15: " + "、".join(f"{k}({v * 100:.1f}%)" for k, v in top), flush=True)

print(f"完成，用时 {(time.time() - started) / 60:.1f} 分钟", flush=True)
