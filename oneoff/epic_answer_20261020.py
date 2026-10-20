# -*- coding: utf-8 -*-
"""Epic 第7行回答：板块行情分时数据断更根因与修复。"""
import openpyxl

PATH = r"D:\kiro-worspace\test_project\stock-system\epic\epic_description.xlsx"

ANSWER_7 = (
    "已完成排查与修复。根因：每日更新链路（daily_update.py）缺少「个股分时」采集步骤，"
    "stock_minute_quote 停在 2026-09-16，而板块分时（boardmin）依赖个股分时加权合成，"
    "因此每日空转、板块行情分时也停在 9-16。"
    "修复：①链路补 minutes 步骤（sync_minute_quotes，boardmin 之前）；②历史回填——"
    "个股分时按窗口 2026-09-10~2026-10-19（22 个交易日）全市场 5225 只回填，约 756 万行、零失败；"
    "板块分时 --boardmin 重新合成，现覆盖最近 30 个交易日（2026-09-01~2026-10-20）无断档、"
    "每日 2352 行。验证：stock_minute_quote 最新 bar_time=2026-10-20 15:00，"
    "09-17 以来每个交易日约 25 万行；/api/boards/{code}/minute_dates 返回 30 天连续日期；"
    "2026-10-20 盘后调度实跑 minutes ✓ boardmin ✓（修复已在自动化中生效）。"
    "注：当日 moneyflow/mfhist 两步采集失败与本题无关，资金流停在 10-19，另行排查。"
)

wb = openpyxl.load_workbook(PATH)
ws = wb.active
row = 7
detail = str(ws.cell(row=row, column=3).value or "")
assert "板块行情" in detail, f"第{row}行关键字校验失败: {detail[:50]}"
ws.cell(row=row, column=5).value = ANSWER_7
wb.save(PATH)
print(f"OK 第{row}行回答已回填")
