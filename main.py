# -*- coding: utf-8 -*-
"""
拼多多草稿箱 批量改价 + 上架 脚本（Playwright 版）

用法：
    python main.py --scan            # 只扫描草稿箱，看有多少条
    python main.py --inspect-edit    # 自动抓取页面结构（调试用）
    python main.py                   # 正式运行（运行时输入价格倍数）
    python main.py --multiplier 1.5 --limit 5   # 指定倍数、最多处理5条

流程：进入草稿箱 → 逐条打开编辑页 → 价格 × 倍数 → 提交并上架 → 下一条。
"""

import argparse
import csv
import math
import os
import random
import re
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

import config

log = None


# ------------------------------------------------------------
# 基础工具
# ------------------------------------------------------------
def setup_logging():
    global log
    import logging
    # 防止打印含特殊字符（如替换符）时报 UnicodeEncodeError
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("run.log", encoding="utf-8"),
        ],
    )
    log = logging.getLogger("pdd")


def human_delay(rng, lo, hi):
    time.sleep(rng.uniform(lo, hi))


def parse_price(s):
    """从字符串里提取数字，返回 float；失败返回 None。"""
    if s is None:
        return None
    m = re.search(r"\d+(\.\d+)?", str(s))
    return float(m.group()) if m else None


def compute_price(old, mult, mode):
    new = old * mult
    if mode == "up":
        return math.ceil(new * 100) / 100.0
    if mode == "down":
        return math.floor(new * 100) / 100.0
    return round(new, 2)


def input_multiplier():
    while True:
        s = input("请输入价格倍数（例如 1.5 表示乘以 1.5）：").strip()
        try:
            v = float(s)
            if v <= 0:
                print("倍数必须大于 0，请重新输入。")
                continue
            return v
        except ValueError:
            print("请输入数字，例如 1.5。")


def record_result(path, title, old, new, status):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["时间", "商品标题", "原价", "新价", "状态"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    title, old, new, status])


# ------------------------------------------------------------
# 浏览器与页面定位
# ------------------------------------------------------------
def launch(p, cfg):
    kwargs = dict(
        user_data_dir=cfg.CHROME_PROFILE_DIR,
        headless=False,
        viewport=cfg.VIEWPORT,
        args=["--disable-blink-features=AutomationControlled"],
    )
    if cfg.USE_SYSTEM_CHROME:
        try:
            return p.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception as e:
            print(f"[!] 使用系统 Chrome 失败（{e}），改用 Playwright 自带浏览器。")
            print("    若仍失败，请先运行：playwright install chromium")
    return p.chromium.launch_persistent_context(**kwargs)


def goto_draft_box(page, cfg):
    """等待登录并进入草稿箱列表（点草稿箱 tab）。"""
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            if page.locator(cfg.SELECTORS["draft_tab"]).count() > 0:
                break
        except Exception:
            pass
        try:
            if "login" in page.url or "passport" in page.url or page.get_by_text("扫码登录").count() > 0:
                print("[!] 检测到登录页，请在浏览器里扫码登录……", flush=True)
        except Exception:
            pass
        time.sleep(3)

    try:
        tab = page.locator(cfg.SELECTORS["draft_tab"]).first
        if tab.count() > 0:
            tab.click()
            time.sleep(3)
            print("[!] 已进入草稿箱", flush=True)
        else:
            print("[!] 未找到草稿箱 tab，请确认已登录并进入商品列表。", flush=True)
    except Exception as e:
        print(f"[!] 点击草稿箱失败：{e}", flush=True)

    try:
        page.wait_for_selector(cfg.SELECTORS["edit_btn"], timeout=30000)
    except Exception:
        print("[!] 草稿列表加载超时。", flush=True)


def get_draft_page(context, cfg):
    """在所有已打开页面里，找草稿箱列表页（含 a.edit-link 的页面）。"""
    for p in context.pages:
        try:
            if p.locator(cfg.SELECTORS["edit_btn"]).count() > 0:
                return p
        except Exception:
            pass
    return None


def wait_if_blocked(context, cfg, timeout=300):
    """检测疑似验证码/风控，发现就自动等你手动处理完（不再用键盘输入）。"""
    for p in context.pages:
        for hint in cfg.CAPTCHA_HINTS:
            try:
                if p.get_by_text(hint).count() > 0:
                    print(f"\n[!] 检测到疑似验证/风控（关键词：{hint}），请在浏览器里手动处理……", flush=True)
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        still = False
                        for pg in context.pages:
                            try:
                                if pg.get_by_text(hint).count() > 0:
                                    still = True
                                    break
                            except Exception:
                                pass
                        if not still:
                            print("    已处理完成，继续。", flush=True)
                            return
                        time.sleep(5)
                    print("    等待超时，继续执行（可能仍有验证）。", flush=True)
                    return
            except Exception:
                pass


def close_extra_tab(context, edit_page, list_page):
    """关闭编辑页标签（如果它还开着）。"""
    if edit_page is not None and edit_page is not list_page:
        try:
            edit_page.close()
        except Exception:
            pass


def read_title(edit_page, cfg):
    t = edit_page.locator(cfg.SELECTORS["title_input"]).first
    if t.count() > 0:
        try:
            return (t.input_value() or "").strip()[:80]
        except Exception:
            pass
    return ""


def read_goods_id(list_page, cfg):
    """读取草稿箱列表第一条商品的 goods-id，用于判断上架是否成功。"""
    g = list_page.locator(cfg.SELECTORS["goods_id"]).first
    if g.count() > 0:
        try:
            return (g.inner_text() or "").strip()
        except Exception:
            pass
    return None


def read_old_sku_prices(edit_page, cfg):
    """读取旧价：第一条 SKU 的 (拼单价, 单买价)。

    SKU 行内“请输入”输入框顺序是 [库存, 拼单价, 单买价, 规格编码]。
    统一改价时所有 SKU 价格一致，取第一条即可。
    """
    row = edit_page.locator(cfg.SELECTORS["sku_row"]).first
    if row.count() == 0:
        return None, None
    cells = row.locator(cfg.SELECTORS["sku_cell_input"]).all()
    if len(cells) >= 3:
        try:
            return parse_price(cells[1].input_value()), parse_price(cells[2].input_value())
        except Exception:
            return None, None
    return None, None


def find_batch_inputs(edit_page, cfg):
    """返回统一改价所需的 (批量拼单价输入框, 批量单买价输入框, “批量设置”按钮)。

    多SKU商品的 SKU 表格是虚拟滚动，逐个改只会改到可见的第一批，
    所以用页面自带的“批量设置”一次应用到所有 SKU。找不到（如单SKU）返回 (None, None, None)。
    """
    p = edit_page.locator(cfg.SELECTORS["batch_price_input"]).first
    d = edit_page.locator(cfg.SELECTORS["batch_single_price_input"]).first
    b = edit_page.locator(cfg.SELECTORS["batch_set_btn"]).first
    if p.count() > 0 and d.count() > 0 and b.count() > 0:
        return p, d, b
    # 兜底：按 placeholder / 文案找
    p = edit_page.locator("input[placeholder='拼单价']").first
    d = edit_page.locator("input[placeholder='单买价']").first
    b = edit_page.locator("button:has-text('批量设置')").first
    if p.count() > 0 and d.count() > 0 and b.count() > 0:
        return p, d, b
    return None, None, None


# ------------------------------------------------------------
# 调试/校准模式
# ------------------------------------------------------------
def dump_page(pg, cfg, idx):
    lines = [f"===== 页面 {idx} =====", f"URL: {pg.url}", ""]
    lines.append("===== 按钮 =====")
    for b in pg.locator("button, a[role=button], a").all()[:200]:
        try:
            txt = (b.inner_text() or "").strip()
            if not txt:
                continue
            tag = b.evaluate("el => el.tagName")
            cls = b.get_attribute("class") or ""
            lines.append(f"  <{tag}>  {txt[:40]}   class={cls[:70]}")
        except Exception:
            pass
    lines.append("")
    lines.append("===== 输入框 =====")
    for i in pg.locator("input, textarea").all()[:200]:
        try:
            ph = i.get_attribute("placeholder") or ""
            typ = i.get_attribute("type") or ""
            lines.append(f"  input type={typ!r} placeholder={ph!r}")
        except Exception:
            pass
    text = "\n".join(lines)
    print(text, flush=True)
    base = f"inspect_{idx}" if idx else "inspect"
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(text)
    try:
        pg.screenshot(path=base + ".png", full_page=True)
    except Exception:
        pass
    try:
        with open(base + ".html", "w", encoding="utf-8") as f:
            f.write(pg.content())
    except Exception:
        pass


def inspect_edit_flow(context, page, cfg):
    """全自动：进草稿箱 → 打开第一条编辑页 → 抓取结构。"""
    goto_draft_box(page, cfg)
    try:
        edit = page.locator(cfg.SELECTORS["edit_btn"]).first
        with context.expect_page(timeout=30000) as ni:
            edit.click()
        new_page = ni.value
        print(f"[!] 已打开编辑页：{new_page.url}", flush=True)
        try:
            new_page.wait_for_load_state("domcontentloaded")
        except Exception:
            pass
        time.sleep(10)
    except Exception as e:
        print(f"[!] 打开编辑页失败：{e}", flush=True)

    for i, pg in enumerate(context.pages):
        try:
            dump_page(pg, cfg, i)
        except Exception as e:
            print(f"页面 {i} 抓取失败：{e}", flush=True)


def scan_mode(context, page, cfg):
    goto_draft_box(page, cfg)
    n = page.locator(cfg.SELECTORS["edit_btn"]).count()
    print(f"\n草稿箱当前共 {n} 条商品。")


# ------------------------------------------------------------
# 核心：处理一条商品
# ------------------------------------------------------------
def process_item(context, list_page, cfg, mult, rng):
    """打开编辑页 → 所有价格 × 倍数 → 提交并上架。返回 (状态, 标题, 原价, 新价)。"""
    title = old_base = new_base = None

    edit = list_page.locator(cfg.SELECTORS["edit_btn"]).first
    if edit.count() == 0:
        return "skip", title, old_base, new_base

    # 1. 打开编辑页（新标签）
    try:
        with context.expect_page(timeout=30000) as ni:
            edit.click()
        edit_page = ni.value
    except Exception as e:
        log.warning(f"打开编辑页失败：{e}")
        return "skip", title, old_base, new_base
    try:
        edit_page.wait_for_load_state("domcontentloaded")
    except Exception:
        pass
    try:
        edit_page.wait_for_selector(cfg.SELECTORS["submit_btn"], timeout=30000)
    except Exception as e:
        log.warning(f"编辑页加载超时：{e}")
        close_extra_tab(context, edit_page, list_page)
        return "skip", title, old_base, new_base

    # 2. 等价格数据回填（批量设置输入框或 SKU 行出现），最多 20 秒
    deadline = time.time() + 20
    while time.time() < deadline:
        if edit_page.locator(cfg.SELECTORS["batch_price_input"]).count() > 0:
            break
        if edit_page.locator(cfg.SELECTORS["sku_row"]).count() > 0:
            break
        time.sleep(1)

    wait_if_blocked(context, cfg)
    title = read_title(edit_page, cfg)

    # 3. 读旧价：拼单价/单买价（取第一条SKU），商品参考价(划线价)单独读
    old_p, old_d = read_old_sku_prices(edit_page, cfg)
    ref_inp = edit_page.locator(cfg.SELECTORS["ref_price_input"]).first
    old_ref = None
    if ref_inp.count() > 0:
        try:
            old_ref = parse_price(ref_inp.input_value())
        except Exception:
            old_ref = None

    if old_p is None and old_d is None:
        log.warning("未读取到 SKU 价格")
        close_extra_tab(context, edit_page, list_page)
        return "skip", title, old_base, new_base

    new_p = compute_price(old_p, mult, cfg.PRICE_ROUND) if old_p else None
    new_d = compute_price(old_d, mult, cfg.PRICE_ROUND) if old_d else None
    new_ref = compute_price(old_ref, mult, cfg.PRICE_ROUND) if old_ref else None
    if old_p is not None:
        old_base, new_base = old_p, new_p

    # 4. 统一改价：用页面自带“批量设置”，一次把 拼单价/单买价 应用到所有 SKU
    batch_p, batch_d, batch_btn = find_batch_inputs(edit_page, cfg)
    if batch_p is not None and new_p is not None:
        try:
            batch_p.click()
            human_delay(rng, *cfg.DELAY_ACTION)
            batch_p.fill(f"{new_p:.2f}")
            human_delay(rng, *cfg.DELAY_ACTION)
            if new_d is not None:
                batch_d.click()
                human_delay(rng, *cfg.DELAY_ACTION)
                batch_d.fill(f"{new_d:.2f}")
                human_delay(rng, *cfg.DELAY_ACTION)
            # 等“批量设置”按钮从禁用变为可点
            deadline = time.time() + 5
            while time.time() < deadline and batch_btn.get_attribute("disabled") is not None:
                time.sleep(0.3)
            if batch_btn.get_attribute("disabled") is None:
                batch_btn.click()
                human_delay(rng, *cfg.DELAY_ACTION)
            else:
                log.warning("批量设置按钮未启用")
        except Exception as e:
            log.warning(f"统一改价(批量设置)失败：{e}")
    else:
        # 兜底：没有批量设置行（单SKU），直接改第一条SKU的 拼单价/单买价
        log.warning("未找到批量设置，改为直接填写第一条SKU价格")
        row = edit_page.locator(cfg.SELECTORS["sku_row"]).first
        cells = row.locator(cfg.SELECTORS["sku_cell_input"]).all()
        for idx, val in ((1, new_p), (2, new_d)):
            if val is None or idx >= len(cells):
                continue
            try:
                cells[idx].click()
                human_delay(rng, *cfg.DELAY_ACTION)
                cells[idx].fill(f"{val:.2f}")
                human_delay(rng, *cfg.DELAY_ACTION)
            except Exception as e:
                log.warning(f"填写价格失败：{e}")

    # 5. 商品参考价(划线价)：保持与单买价同样的倍数关系
    if ref_inp.count() > 0 and new_ref is not None:
        try:
            ref_inp.click()
            human_delay(rng, *cfg.DELAY_ACTION)
            ref_inp.fill(f"{new_ref:.2f}")
            human_delay(rng, *cfg.DELAY_ACTION)
        except Exception as e:
            log.warning(f"填写参考价失败：{e}")

    # 6. 提交并上架
    submit = edit_page.locator(cfg.SELECTORS["submit_btn"]).first
    if submit.count() == 0:
        log.warning("未找到“提交并上架”按钮")
        close_extra_tab(context, edit_page, list_page)
        return "skip", title, old_base, new_base
    try:
        submit.click()
    except Exception as e:
        log.warning(f"点击“提交并上架”失败：{e}")
        close_extra_tab(context, edit_page, list_page)
        return "skip", title, old_base, new_base

    # 7. 等待提交完成：跳转到 success 页 或 出现“提交成功”，最多 90 秒
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if edit_page.is_closed():
                break
            if "success" in edit_page.url:
                break
            if edit_page.get_by_text("提交成功").count() > 0:
                break
        except Exception:
            break
        time.sleep(2)
    wait_if_blocked(context, cfg)

    # 8. 关闭编辑标签页
    close_extra_tab(context, edit_page, list_page)
    return "ok", title, old_base, new_base


# ------------------------------------------------------------
# 主循环
# ------------------------------------------------------------
def run_mode(context, page, cfg, mult, rng, limit):
    goto_draft_box(page, cfg)
    total = 0
    consec_fail = 0

    while True:
        if limit is not None and total >= limit:
            break
        wait_if_blocked(context, cfg)

        list_page = get_draft_page(context, cfg)
        if not list_page:
            print("没有找到草稿箱页面，结束。")
            break

        edit_btns = list_page.locator(cfg.SELECTORS["edit_btn"])
        if edit_btns.count() == 0:
            print("草稿箱已清空，结束。")
            break
        first_gid = read_goods_id(list_page, cfg)

        status, title, old, new = process_item(context, list_page, cfg, mult, rng)
        total += 1
        record_result(cfg.RESULT_CSV, title, old, new, status)
        print(f"[{total}] {title or '(无标题)'}  原价 {old} → 新价 {new}  状态：{status}")

        # 刷新列表页并重新进草稿箱（上架成功的商品会离开草稿箱）
        try:
            list_page.bring_to_front()
            list_page.reload(wait_until="domcontentloaded")
            goto_draft_box(list_page, cfg)
        except Exception as e:
            log.warning(f"刷新列表失败：{e}")

        # 判断上架是否成功：上架成功的话，第一条商品的 goods-id 会变
        after_gid = read_goods_id(list_page, cfg)
        if status == "ok" and first_gid and after_gid and first_gid == after_gid:
            consec_fail += 1
            print(f"    [!] 该商品可能仍在草稿箱（上架失败），连续 {consec_fail} 次")
            if consec_fail >= 2:
                print("    连续失败，请手动检查该商品后重新运行。已停止。")
                break
        else:
            consec_fail = 0

        human_delay(rng, *cfg.DELAY_ITEM)
        if total % cfg.BATCH_SIZE == 0:
            input(f"\n已处理 {total} 条。按回车继续下一批，或 Ctrl+C 退出……")

    print(f"\n完成，共处理 {total} 条。结果见 {cfg.RESULT_CSV}")


# ------------------------------------------------------------
# 入口
# ------------------------------------------------------------
def main():
    setup_logging()

    ap = argparse.ArgumentParser(description="拼多多草稿箱批量改价+上架")
    ap.add_argument("--scan", action="store_true", help="只扫描草稿箱数量，不做修改")
    ap.add_argument("--inspect-edit", action="store_true", help="自动抓取页面结构（调试用）")
    ap.add_argument("--multiplier", type=float, default=None, help="价格倍数（也可运行时输入）")
    ap.add_argument("--limit", type=int, default=None, help="最多处理多少条")
    ap.add_argument("--url", default=None, help="覆盖 config.py 里的草稿箱地址")
    args = ap.parse_args()

    mult = args.multiplier or config.PRICE_MULTIPLIER
    if mult is None and not (args.scan or args.inspect_edit):
        mult = input_multiplier()
        print(f"价格倍数：{mult}")

    rng = random.Random()

    with sync_playwright() as p:
        context = launch(p, config)
        try:
            page = context.pages[0] if context.pages else context.new_page()

            url = args.url or config.DRAFT_BOX_URL
            print(f"正在打开：{url}", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                print(f"[!] 打开页面失败：{e}", flush=True)

            if args.inspect_edit:
                inspect_edit_flow(context, page, config)
                return

            if args.scan:
                scan_mode(context, page, config)
                return

            run_mode(context, page, config, mult, rng, args.limit)
        finally:
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
