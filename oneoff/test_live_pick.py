# -*- coding: utf-8 -*-
"""盘中实时选股端到端验证(2026-09-23): pick_live 落盘查看, 不落库。"""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.pick_service import pick_live  # noqa: E402

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_test.log")
r = pick_live("VOLUME_BREAKOUT")
with io.open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write(json.dumps({k: v for k, v in r.items() if k != "items"},
                       ensure_ascii=False, default=str) + "\n")
    for i in r["items"][:8]:
        f.write("%s %s %s %s score=%s %s\n" % (
            i["ts_code"], i["name"], i["close_price"], i["pct_chg"],
            i["score"], (i["hit_reason"] or "")[:70]))
print("DONE", len(r["items"]))
